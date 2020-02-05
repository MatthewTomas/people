#!/usr/bin/env python
import os
import sys
import glob
from functools import lru_cache
from django.db import transaction
import click
import openstates_metadata as metadata

try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader as Loader

from utils import (
    get_data_dir,
    get_jurisdiction_id,
    get_all_abbreviations,
    load_yaml,
    legacy_districts,
    init_django,
)


class CancelTransaction(Exception):
    pass


@lru_cache(128)
def cached_lookup(ModelCls, **kwargs):
    return ModelCls.objects.get(**kwargs)


def update_subobjects(person, fieldname, objects, read_manager=None):
    """ returns True if there are any updates """
    # we need the default manager for this field in case we need to do updates
    manager = getattr(person, fieldname)

    # if a read_manager is passed, we'll use that for all read operations
    # this is used for Person.memberships to ensure we don't wipe out committee memberships
    if read_manager is None:
        read_manager = manager

    current_count = read_manager.count()
    updated = False

    # if counts differ, we need to do an update for sure
    if current_count != len(objects):
        updated = True

    # check if all objects exist
    if not updated:
        qs = read_manager
        for obj in objects:
            qs = qs.exclude(**obj)

        if qs.exists():
            updated = True

    # if there's been an update, wipe the old & insert the new
    if updated:
        if current_count:
            read_manager.all().delete()
        for obj in objects:
            manager.create(**obj)
        # save to bump updated_at timestamp
        person.save()

    return updated


def get_update_or_create(ModelCls, data, lookup_keys):
    updated = created = False
    kwargs = {k: data[k] for k in lookup_keys}
    try:
        obj = ModelCls.objects.get(**kwargs)
        for field, value in data.items():
            if getattr(obj, field) != value:
                setattr(obj, field, value)
                updated = True
        if updated:
            obj.save()
    except ModelCls.DoesNotExist:
        obj = ModelCls.objects.create(**data)
        created = True
    return obj, created, updated


def load_person(data):
    # import has to be here so that Django is set up
    from opencivicdata.core.models import Person, Organization, Post

    fields = dict(
        id=data["id"],
        name=data["name"],
        given_name=data.get("given_name", ""),
        family_name=data.get("family_name", ""),
        gender=data.get("gender", ""),
        biography=data.get("biography", ""),
        birth_date=data.get("birth_date", ""),
        death_date=data.get("death_date", ""),
        image=data.get("image", ""),
        extras=data.get("extras", {}),
    )
    person, created, updated = get_update_or_create(Person, fields, ["id"])

    updated |= update_subobjects(person, "other_names", data.get("other_names", []))
    updated |= update_subobjects(person, "links", data.get("links", []))
    updated |= update_subobjects(person, "sources", data.get("sources", []))

    identifiers = []
    for scheme, value in data.get("ids", {}).items():
        identifiers.append({"scheme": scheme, "identifier": value})
    for identifier in data.get("other_identifiers", []):
        identifiers.append(identifier)
    updated |= update_subobjects(person, "identifiers", identifiers)

    contact_details = []
    for cd in data.get("contact_details", []):
        for type in ("address", "email", "voice", "fax"):
            if cd.get(type):
                contact_details.append(
                    {"note": cd.get("note", ""), "type": type, "value": cd[type]}
                )
    updated |= update_subobjects(person, "contact_details", contact_details)

    memberships = []
    for party in data.get("party", []):
        try:
            org = cached_lookup(Organization, classification="party", name=party["name"])
        except Organization.DoesNotExist:
            click.secho(f"no such party {party['name']}", fg="red")
            raise CancelTransaction()
        memberships.append(
            {
                "organization": org,
                "start_date": party.get("start_date", ""),
                "end_date": party.get("end_date", ""),
            }
        )
    for role in data.get("roles", []):
        if role["type"] in ("upper", "lower", "legislature"):
            try:
                org = cached_lookup(
                    Organization, classification=role["type"], jurisdiction_id=role["jurisdiction"]
                )
                post = org.posts.get(label=role["district"])
            except Organization.DoesNotExist:
                click.secho(
                    f"{person} no such organization {role['jurisdiction']} {role['type']}",
                    fg="red",
                )
                raise CancelTransaction()
            except Post.DoesNotExist:
                # if this is a legacy district, be quiet
                lds = legacy_districts(jurisdiction_id=role["jurisdiction"])
                if role["district"] in lds[role["type"]]:
                    continue
                click.secho(f"no such post {role}", fg="red")
                raise CancelTransaction()
        else:
            raise ValueError("unsupported role type")
        memberships.append(
            {
                "organization": org,
                "post": post,
                "start_date": role.get("start_date", ""),
                "end_date": role.get("end_date", ""),
            }
        )

    # note that we don't manager committee memberships here
    updated |= update_subobjects(
        person,
        "memberships",
        memberships,
        read_manager=person.memberships.exclude(organization__classification="committee"),
    )

    return created, updated


def load_org(data):
    from opencivicdata.core.models import Organization, Person

    parent_id = data["parent"]
    if parent_id.startswith("ocd-organization"):
        parent = Organization.objects.get(pk=parent_id)
    else:
        parent = Organization.objects.get(
            jurisdiction_id=data["jurisdiction"], classification=parent_id
        )

    fields = dict(
        id=data["id"],
        name=data["name"],
        jurisdiction_id=data["jurisdiction"],
        classification=data["classification"],
        founding_date=data.get("founding_date", ""),
        dissolution_date=data.get("dissolution_date", ""),
        parent=parent,
    )
    org, created, updated = get_update_or_create(Organization, fields, ["id"])

    updated |= update_subobjects(org, "links", data.get("links", []))
    updated |= update_subobjects(org, "sources", data.get("sources", []))

    memberships = []
    for role in data.get("memberships", []):
        if role.get("id"):
            try:
                person = Person.objects.get(pk=role["id"])
            except Person.DoesNotExist:
                click.secho(f"no such person {role['id']}", fg="red")
                raise CancelTransaction()
        else:
            person = None

        memberships.append(
            {
                "person": person,
                "person_name": role["name"],
                "role": role.get("role", "member"),
                "start_date": role.get("start_date", ""),
                "end_date": role.get("end_date", ""),
            }
        )
    updated |= update_subobjects(org, "memberships", memberships)

    return created, updated


def sort_organizations(orgs):
    order = []
    seen = set()
    how_many = len(orgs)

    while orgs:
        for org, filename in list(orgs):
            if (org["parent"].startswith("ocd-organization") and org["parent"] in seen) or not org[
                "parent"
            ].startswith("ocd-organization"):
                seen.add(org["id"])
                order.append((org, filename))
                orgs.remove((org, filename))

    # TODO: this doesn't check for infinite loops when two orgs refer to one another
    assert len(order) == how_many

    return order


def _echo_org_status(org, created, updated):
    if created:
        click.secho(f"{org} created", fg="green")
    elif updated:
        click.secho(f"{org} updated", fg="yellow")


def create_juris_orgs_posts(jurisdiction_id):
    from opencivicdata.core.models import Organization, Jurisdiction

    state = metadata.lookup(jurisdiction_id=jurisdiction_id)

    juris, _ = Jurisdiction.objects.update_or_create(
        id=state.jurisdiction_id,
        defaults={
            "name": state.name,
            "url": state.url,
            "classification": "government",
            "division_id": state.division_id,
        },
    )

    for chamber in state.chambers:
        org, _ = Organization.objects.update_or_create(
            # TODO: restore ID here
            # id=chamber.organization_id
            jurisdiction=juris,
            classification="legislature"
            if chamber.chamber_type == "unicameral"
            else chamber.chamber_type,
            defaults={"name": chamber.name},
        )

        # add posts to org
        posts = [
            {
                "label": d.name,
                "role": chamber.title,
                "division_id": d.division_id,
                "maximum_memberships": d.num_seats,
            }
            for d in chamber.districts
        ]
        updated = update_subobjects(org, "posts", posts)
        if updated:
            click.secho(f"updated {org} posts", fg="yellow")


def load_directory(files, type, jurisdiction_id, purge):
    ids = set()
    merged = {}
    created_count = 0
    updated_count = 0

    if type == "person":
        from opencivicdata.core.models import Person
        from opencivicdata.legislative.models import BillSponsorship

        existing_ids = set(
            Person.objects.filter(
                memberships__organization__jurisdiction_id=jurisdiction_id
            ).values_list("id", flat=True)
        )
        ModelCls = Person
        load_func = load_person
    elif type == "organization":
        from opencivicdata.core.models import Organization

        existing_ids = set(
            Organization.objects.filter(
                jurisdiction_id=jurisdiction_id, classification="committee"
            ).values_list("id", flat=True)
        )
        ModelCls = Organization
        load_func = load_org
    else:
        raise ValueError(type)

    all_data = []
    for filename in files:
        with open(filename) as f:
            data = load_yaml(f)
            all_data.append((data, filename))

    if type == "organization":
        all_data = sort_organizations(all_data)

    for data, filename in all_data:
        ids.add(data["id"])
        created, updated = load_func(data)

        if created:
            click.secho(f"created {type} from {filename}", fg="cyan", bold=True)
            created_count += 1
        elif updated:
            click.secho(f"updated {type} from {filename}", fg="cyan")
            updated_count += 1

    missing_ids = existing_ids - ids

    # check if missing ids are in need of a merge
    for missing_id in missing_ids:
        try:
            found = ModelCls.objects.get(
                identifiers__identifier=missing_id, identifiers__scheme="openstates"
            )
            merged[missing_id] = found.id
        except ModelCls.DoesNotExist:
            pass

    if merged:
        click.secho(f"{len(merged)} removed via merge", fg="yellow")
        for old, new in merged.items():
            click.secho(f"   {old} => {new}", fg="yellow")
            BillSponsorship.objects.filter(person_id=old).update(person_id=new)
            ModelCls.objects.filter(id=old).delete()
            missing_ids.remove(old)

    # ids that are still missing would need to be purged
    if missing_ids and not purge:
        click.secho(f"{len(missing_ids)} went missing, run with --purge to remove", fg="red")
        for id in missing_ids:
            mobj = ModelCls.objects.get(pk=id)
            click.secho(f"  {id}: {mobj}")
        raise CancelTransaction()
    elif missing_ids and purge:
        click.secho(f"{len(missing_ids)} purged", fg="yellow")
        ModelCls.objects.filter(id__in=missing_ids).delete()

    click.secho(
        f"processed {len(ids)} {type} files, {created_count} created, " f"{updated_count} updated",
        fg="green",
    )


def create_parties():
    from opencivicdata.core.models import Organization

    settings_file = os.path.join(os.path.dirname(__file__), "../settings.yml")
    with open(settings_file) as f:
        settings = load_yaml(f)
    parties = settings["parties"]
    for party in parties:
        org, created = Organization.objects.get_or_create(name=party, classification="party")
        if created:
            click.secho(f"created party: {party}", fg="green")



@click.command()
@click.argument("abbreviations", nargs=-1)
@click.option(
    "--purge/--no-purge", default=False, help="Purge all legislators from DB that aren't in YAML."
)
@click.option(
    "--safe/--no-safe",
    default=False,
    help="Operate in safe mode, no changes will be written to database.",
)
def to_database(abbreviations, purge, safe):
    """
    Sync YAML files to DB.
    """
    init_django()

    create_parties()

    if not abbreviations:
        abbreviations = get_all_abbreviations()

    for abbr in abbreviations:
        click.secho("==== {} ====".format(abbr), bold=True)
        directory = get_data_dir(abbr)
        jurisdiction_id = get_jurisdiction_id(abbr)

        person_files = glob.glob(os.path.join(directory, "people/*.yml")) + glob.glob(
            os.path.join(directory, "retired/*.yml")
        )
        committee_files = glob.glob(os.path.join(directory, "organizations/*.yml"))

        if safe:
            click.secho("running in safe mode, no changes will be made", fg="magenta")

        try:
            with transaction.atomic():
                create_juris_orgs_posts(jurisdiction_id)
                load_directory(person_files, "person", jurisdiction_id, purge=purge)
                load_directory(committee_files, "organization", jurisdiction_id, purge=purge)
                if safe:
                    click.secho("ran in safe mode, no changes were made", fg="magenta")
                    raise CancelTransaction()
        except CancelTransaction:
            sys.exit(1)


if __name__ == "__main__":
    to_database()

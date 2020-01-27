import scrapelib
import lxml.html
from functools import lru_cache


class Page(scrapelib.Scraper):
    """
    Base class for scrapers.

    One subclass should be written per type of page on the site.

    url:
        can be provided at class level or passed in
    obj:
        passed in if this is a subpage
    """

    def __init__(self, *, url=None, obj=None):
        super().__init__()
        self.obj = obj
        if url or not hasattr(self, "url"):
            self.url = url
        self.doc = None

    @lru_cache(maxsize=None)
    def lxml(self, url):
        """
        method that actually fetches the data, might be called by a child class
        """
        print(f"fetching {url} via {self.__class__.__name__}")
        html = self.get(url)
        doc = lxml.html.fromstring(html.content)
        doc.make_links_absolute(url)
        return doc

    def fetch(self, *, using=None):
        if not self.url:
            self.url = self.get_url()
        if not using:
            using = self
        self.doc = using.lxml(self.url)


class ListPage(Page):
    def _get_items(self):
        if self.doc is None:
            self.doc = self.lxml(self.url)

        items = self.doc.xpath(self.list_xpath)
        if not items:
            raise ValueError(f"no items for {self.list_xpath} on {self.url}")
        return items

    def scrape(self):
        """ called when using ListPage as a subpage """
        for item in self._get_items():
            self.handle_list_item(item)

    def yield_objects(self):
        """ called as entrypoint, yields resulting items """
        for item in self._get_items():
            obj = self.handle_list_item(item)
            if obj:
                for PageCls in self.detail_pages:
                    page = PageCls(obj=obj)
                    page.fetch(using=self)
                    page.scrape()
                yield obj

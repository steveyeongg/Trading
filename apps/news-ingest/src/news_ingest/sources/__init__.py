"""News sources. Each implements `async fetch() -> list[RawItem]`."""

from news_ingest.sources.base import NewsSource, RawItem
from news_ingest.sources.file_source import FileSource
from news_ingest.sources.newsapi import NewsApiSource
from news_ingest.sources.rss import RssSource

__all__ = ["FileSource", "NewsApiSource", "NewsSource", "RawItem", "RssSource"]

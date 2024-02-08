import json
import os
import requests
import re
import markdownify
import io
import uuid
import mimetypes
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from dataclasses import dataclass
from typing import Dict, List, Optional, Union, Callable, Literal, Tuple

# Optional PDF support
IS_PDF_CAPABLE = False
try:
    import pdfminer
    import pdfminer.high_level

    IS_PDF_CAPABLE = True
except ModuleNotFoundError:
    pass

# Other optional dependencies
try:
    import pathvalidate
except ModuleNotFoundError:
    pass


class TextRendererResult:
    """The result of rendering a webpage to text."""

    def __init__(self, title: Union[str, None] = None, page_content: str = ""):
        self.title = title
        self.page_content = page_content


class PageTextRenderer:
    """A TextRender is used by the SimpleTextBrowser to claim
    responsibility for rendering a page. Once a page has been claimed,
    the instance' render_page function will be called, and the result
    stream is expected to be consumed -- there is no going back."""

    def claim_responsibility(self, url, status_code, content_type, **kwargs) -> bool:
        """Return true only if the text renderer is prepared to
        claim responsibility for the page.
        """
        raise NotImplementedError()

    def render_page(self, response, url, status_code, content_type) -> TextRendererResult:
        """Return true only if the text renderer is prepared to
        claim responsibility for the page.
        """
        raise NotImplementedError()

    # Helper functions
    def _read_all_text(self, response):
        """Read the entire response, and return as a string."""
        text = ""
        for chunk in response.iter_content(chunk_size=512, decode_unicode=True):
            text += chunk
        return text

    def _read_all_html(self, response):
        """Read the entire response, and return as a beautiful soup object."""
        return BeautifulSoup(self._read_all_text(response), "html.parser")

    def _read_all_bytesio(self, response):
        """Read the entire response, and return an in-memory bytes stream."""
        return io.BytesIO(response.raw.read())


class PlainTextRenderer(PageTextRenderer):
    """Anything with content type text/plain"""

    def claim_responsibility(self, url, status_code, content_type, **kwargs) -> bool:
        return content_type is not None and "text/plain" in content_type.lower()

    def render_page(self, response, url, status_code, content_type) -> TextRendererResult:
        return TextRendererResult(title=None, page_content=self._read_all_text(response))


class HtmlRenderer(PageTextRenderer):
    """Anything with content type text/html"""

    def claim_responsibility(self, url, status_code, content_type, **kwargs) -> bool:
        return content_type is not None and "text/html" in content_type.lower()

    def render_page(self, response, url, status_code, content_type) -> TextRendererResult:
        soup = self._read_all_html(response)

        # Remove javascript and style blocks
        for script in soup(["script", "style"]):
            script.extract()

        webpage_text = markdownify.MarkdownConverter().convert_soup(soup)

        # Convert newlines
        webpage_text = re.sub(r"\r\n", "\n", webpage_text)

        return TextRendererResult(
            title=soup.title.string,
            page_content=re.sub(r"\n{2,}", "\n\n", webpage_text).strip(),  # Remove excessive blank lines
        )


class PdfRenderer(PageTextRenderer):
    """Anything with content type application/pdf"""

    def claim_responsibility(self, url, status_code, content_type, **kwargs) -> bool:
        return content_type is not None and "application/pdf" in content_type.lower()

    def render_page(self, response, url, status_code, content_type) -> TextRendererResult:
        return TextRendererResult(
            title=None,
            page_content=pdfminer.high_level.extract_text(self._read_all_bytesio(response)),
        )


class DownloadRenderer(PageTextRenderer):
    def __init__(self, browser):
        self._browser = browser

    """Catch all downloader, when a download folder is set."""

    def claim_responsibility(self, url, status_code, content_type, **kwargs) -> bool:
        return bool(self._browser.downloads_folder)

    def render_page(self, response, url, status_code, content_type) -> TextRendererResult:
        # Try producing a safe filename
        fname = None
        try:
            fname = pathvalidate.sanitize_filename(os.path.basename(urlparse(url).path)).strip()
        except NameError:
            pass

        # No suitable name, so make one
        if fname is None:
            extension = mimetypes.guess_extension(content_type)
            if extension is None:
                extension = ".download"
            fname = str(uuid.uuid4()) + extension

        # Open a file for writing
        download_path = os.path.abspath(os.path.join(self._browser.downloads_folder, fname))
        with open(download_path, "wb") as fh:
            for chunk in response.iter_content(chunk_size=512):
                fh.write(chunk)

        return TextRendererResult(
            title="Download complete.",
            page_content=f"Downloaded '{url}' to '{download_path}'.",
        )


class FallbackPageRenderer(PageTextRenderer):
    """Accept all requests that come to it."""

    def claim_responsibility(self, url, status_code, content_type, **kwargs) -> bool:
        return True

    def render_page(self, response, url, status_code, content_type) -> TextRendererResult:
        return TextRendererResult(
            title=f"Error - Unsupported Content-Type '{content_type}'",
            page_content=f"Error - Unsupported Content-Type '{content_type}'",
        )


class FallbackErrorRenderer(PageTextRenderer):
    def __init__(self):
        self._html_renderer = HtmlRenderer()

    """Accept all requests that come to it."""

    def claim_responsibility(self, url, status_code, content_type, **kwargs) -> bool:
        return True

    def render_page(self, response, url, status_code, content_type) -> TextRendererResult:
        # If the error was rendered in HTML we might as well render it
        if content_type is not None and "text/html" in content_type.lower():
            res = self._html_renderer.render_page(response, url, status_code, content_type)
            res.title = f"Error {status_code}"
            res.page_content = f"## Error {status_code}\n\n{res.page_content}"
            return res
        else:
            return TextRendererResult(
                title=f"Error {status_code}",
                page_content=f"## Error {status_code}\n\n{self._read_all_text(response)}",
            )


class SimpleTextBrowser:
    """(In preview) An extremely simple text-based web browser comparable to Lynx. Suitable for Agentic use."""

    def __init__(
        self,
        start_page: Optional[str] = "about:blank",
        viewport_size: Optional[int] = 1024 * 8,
        downloads_folder: Optional[Union[str, None]] = None,
        bing_api_key: Optional[Union[str, None]] = None,
        request_kwargs: Optional[Union[Dict, None]] = None,
    ):
        self.start_page = start_page
        self.viewport_size = viewport_size  # Applies only to the standard uri types
        self.downloads_folder = downloads_folder
        self.history = list()
        self.page_title = None
        self.viewport_current_page = 0
        self.viewport_pages = list()
        self.set_address(start_page)
        self.bing_api_key = bing_api_key
        self.request_kwargs = request_kwargs

        self._page_renderers = []
        self._error_renderers = []
        self._page_content = ""

        # Register renderers for successful browsing operations
        # Later registrations are tried first / take higher priority than earlier registrations
        self.register_page_renderer(FallbackPageRenderer())
        self.register_page_renderer(DownloadRenderer(self))
        self.register_page_renderer(HtmlRenderer())
        self.register_page_renderer(PlainTextRenderer())

        if IS_PDF_CAPABLE:
            self.register_page_renderer(PdfRenderer())

        # Register renderers for error conditions
        self.register_error_renderer(FallbackErrorRenderer())

    @property
    def address(self) -> str:
        """Return the address of the current page."""
        return self.history[-1]

    def set_address(self, uri_or_path):
        self.history.append(uri_or_path)

        # Handle special URIs
        if uri_or_path == "about:blank":
            self._set_page_content("")
        elif uri_or_path.startswith("bing:"):
            self._bing_search(uri_or_path[len("bing:") :].strip())
        else:
            if not uri_or_path.startswith("http:") and not uri_or_path.startswith("https:"):
                uri_or_path = urljoin(self.address, uri_or_path)
                self.history[-1] = uri_or_path  # Update the address with the fully-qualified path
            self._fetch_page(uri_or_path)

        self.viewport_current_page = 0

    @property
    def viewport(self) -> str:
        """Return the content of the current viewport."""
        bounds = self.viewport_pages[self.viewport_current_page]
        return self.page_content[bounds[0] : bounds[1]]

    @property
    def page_content(self) -> str:
        """Return the full contents of the current page."""
        return self._page_content

    def _set_page_content(self, content) -> str:
        """Sets the text content of the current page."""
        self._page_content = content
        self._split_pages()
        if self.viewport_current_page >= len(self.viewport_pages):
            self.viewport_current_page = len(self.viewport_pages) - 1

    def page_down(self):
        self.viewport_current_page = min(self.viewport_current_page + 1, len(self.viewport_pages) - 1)

    def page_up(self):
        self.viewport_current_page = max(self.viewport_current_page - 1, 0)

    def visit_page(self, path_or_uri):
        """Update the address, visit the page, and return the content of the viewport."""
        self.set_address(path_or_uri)
        return self.viewport

    def register_page_renderer(self, renderer: PageTextRenderer):
        """Register a page text renderer."""
        self._page_renderers.insert(0, renderer)

    def register_error_renderer(self, renderer: PageTextRenderer):
        """Register a page text renderer."""
        self._error_renderers.insert(0, renderer)

    def _split_pages(self):
        # Split only regular pages
        if not self.address.startswith("http:") and not self.address.startswith("https:"):
            self.viewport_pages = [(0, len(self._page_content))]
            return

        # Handle empty pages
        if len(self._page_content) == 0:
            self.viewport_pages = [(0, 0)]
            return

        # Break the viewport into pages
        self.viewport_pages = []
        start_idx = 0
        while start_idx < len(self._page_content):
            end_idx = min(start_idx + self.viewport_size, len(self._page_content))
            # Adjust to end on a space
            while end_idx < len(self._page_content) and self._page_content[end_idx - 1] not in [" ", "\t", "\r", "\n"]:
                end_idx += 1
            self.viewport_pages.append((start_idx, end_idx))
            start_idx = end_idx

    def _bing_api_call(self, query):
        # Make sure the key was set
        if self.bing_api_key is None:
            raise ValueError("Missing Bing API key.")

        # Prepare the request parameters
        request_kwargs = self.request_kwargs.copy() if self.request_kwargs is not None else {}

        if "headers" not in request_kwargs:
            request_kwargs["headers"] = {}
        request_kwargs["headers"]["Ocp-Apim-Subscription-Key"] = self.bing_api_key

        if "params" not in request_kwargs:
            request_kwargs["params"] = {}
        request_kwargs["params"]["q"] = query
        request_kwargs["params"]["textDecorations"] = False
        request_kwargs["params"]["textFormat"] = "raw"

        request_kwargs["stream"] = False

        # Make the request
        response = requests.get("https://api.bing.microsoft.com/v7.0/search", **request_kwargs)
        response.raise_for_status()
        results = response.json()

        return results

    def _bing_search(self, query):
        results = self._bing_api_call(query)

        web_snippets = list()
        idx = 0
        for page in results["webPages"]["value"]:
            idx += 1
            web_snippets.append(f"{idx}. [{page['name']}]({page['url']})\n{page['snippet']}")
            if "deepLinks" in page:
                for dl in page["deepLinks"]:
                    idx += 1
                    web_snippets.append(
                        f"{idx}. [{dl['name']}]({dl['url']})\n{dl['snippet'] if 'snippet' in dl else ''}"
                    )

        news_snippets = list()
        if "news" in results:
            for page in results["news"]["value"]:
                idx += 1
                news_snippets.append(f"{idx}. [{page['name']}]({page['url']})\n{page['description']}")

        self.page_title = f"{query} - Search"

        content = (
            f"A Bing search for '{query}' found {len(web_snippets) + len(news_snippets)} results:\n\n## Web Results\n"
            + "\n\n".join(web_snippets)
        )
        if len(news_snippets) > 0:
            content += "\n\n## News Results:\n" + "\n\n".join(news_snippets)
        self._set_page_content(content)

    def _fetch_page(self, url):
        try:
            # Prepare the request parameters
            request_kwargs = self.request_kwargs.copy() if self.request_kwargs is not None else {}
            request_kwargs["stream"] = True

            # Send a HTTP request to the URL
            response = requests.get(url, **request_kwargs)
            response.raise_for_status()

            # If the HTTP request was successful
            content_type = response.headers.get("content-type", "")
            for renderer in self._page_renderers:
                if renderer.claim_responsibility(url, response.status_code, content_type):
                    res = renderer.render_page(response, url, response.status_code, content_type)
                    self.page_title = res.title
                    self._set_page_content(res.page_content)
                    return

            # Unhandled page
            self.page_title = "Error - Unhandled _fetch_page"
            self._set_page_content(
                f"""Error - Unhandled _fetch_page:
Url: {url}
Status code: {response.status_code}
Content-type: {content_type}"""
            )
        except requests.exceptions.RequestException as ex:
            for renderer in self._error_renderers:
                response = ex.response
                content_type = response.headers.get("content-type", "")
                if renderer.claim_responsibility(url, response.status_code, content_type):
                    res = renderer.render_page(response, url, response.status_code, content_type)
                    self.page_title = res.title
                    self._set_page_content(res.page_content)
                    return
            self.page_title = "Error - Unhandled _fetch_page"
            self._set_page_content(
                f"""Error - Unhandled _fetch_page error:
Url: {url}
Status code: {response.status_code}
Content-type: {content_type}"""
            )


####################################3
if __name__ == "__main__":
    browser = SimpleTextBrowser()
    # print(browser.visit_page("https://www.adamfourney.com/papers/jahanbakhsh_cscw2022.pdf"))
    print(browser.visit_page("http://www.adamfourney.com"))

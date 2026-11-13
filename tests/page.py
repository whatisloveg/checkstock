from dataclasses import dataclass, field
from html.parser import HTMLParser

import httpx


@dataclass
class Form:
    action: str
    version: str
    autocomplete: str = ""
    buttons: list[str] = field(default_factory=list)
    fields: list[tuple[str, str]] = field(default_factory=list)
    options: dict[str, list[str]] = field(default_factory=dict)

    def value(self, name: str) -> str:
        return dict(self.fields)[name]


# тесты отправляют формы так же, как браузер: со значениями из страницы
class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.version = ""
        self.forms: list[Form] = []
        self.rows: dict[str, dict[str, list[str]]] = {}
        self.form: Form | None = None
        self.select: tuple[str, list[str]] | None = None
        self.option = ""
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.text: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name: value or "" for name, value in attrs}
        if "data-version" in attr:
            self.version = attr["data-version"]
            self.rows[self.version] = {}
        if "data-row" in attr:
            self.row = self.rows[self.version].setdefault(attr["data-row"], [])
        if tag in ("td", "th"):
            self.cell = []
        if tag in ("button", "option"):
            self.text = []
        if tag == "form":
            self.form = Form(attr["action"], self.version, attr.get("autocomplete", ""))
            self.forms.append(self.form)
        elif self.form is None:
            return
        elif tag == "input" and "name" in attr:
            self.form.fields.append((attr["name"], attr.get("value", "")))
        elif tag == "select":
            self.select = (attr["name"], [])
            self.form.options[attr["name"]] = []
        elif tag == "option" and self.select:
            self.option = attr["value"]
            # без selected браузер отправляет первый вариант
            if "selected" in attr or not self.select[1]:
                self.select[1][:] = [attr["value"]]

    def handle_data(self, data: str) -> None:
        for buffer in (self.cell, self.text):
            if buffer is not None:
                buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        text = " ".join("".join(self.text or []).split())
        if tag in ("td", "th") and self.row is not None:
            self.row.append(" ".join("".join(self.cell or []).split()))
            self.cell = None
        elif tag == "tr":
            self.row = None
        elif tag == "form":
            self.form = None
        if self.form is None:
            return
        if tag == "button":
            self.form.buttons.append(text)
        elif tag == "option" and self.select:
            self.form.options[self.select[0]].append(text)
        elif tag == "select" and self.select:
            name, chosen = self.select
            self.form.fields.append((name, chosen[0] if chosen else ""))
            self.select = None


def parse(html: str) -> PageParser:
    parser = PageParser()
    parser.feed(html)
    return parser


def rows(html: str, version: str = "desktop") -> dict[str, list[str]]:
    return parse(html).rows[version]


def find_all(html: str, button: str, version: str = "desktop") -> list[Form]:
    return [f for f in parse(html).forms if f.version == version and button in f.buttons]


def find_form(html: str, button: str, version: str = "desktop", action: str = "") -> Form:
    found = [form for form in find_all(html, button, version) if form.action.endswith(action)]
    assert found, f"нет кнопки {button!r} в версии {version}"
    return found[0]


async def send(client: httpx.AsyncClient, form: Form, **values: object) -> httpx.Response:
    data: dict[str, list[str]] = {}
    for name, value in form.fields:
        data.setdefault(name, []).append(str(values.get(name, value)))
    return await client.post(form.action, data=data)


async def submit(
    client: httpx.AsyncClient,
    page: str,
    button: str,
    version: str = "desktop",
    action: str = "",
    **values: object,
) -> httpx.Response:
    form = find_form((await client.get(page)).text, button, version, action)
    return await send(client, form, **values)

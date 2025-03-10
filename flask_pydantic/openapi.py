import re
import inspect
from functools import wraps
from typing import Callable, List, Optional, Type

from flask import Flask, render_template
from flask.views import MethodView
from werkzeug.routing import parse_converter_args
from pydantic import BaseModel

OPENAPI_VERSION = "3.0.2"
OPENAPI_INFO = dict(
    title="Service Documents",
    version="latest",
)

OPENAPI_NAME = "docs"
OPENAPI_ENDPOINT = "/docs/new/"
OPENAPI_URL_PREFIX = None
OPENAPI_MODE = "normal"

OPENAPI_TEMPLATE_FOLDER = "templates"
OPENAPI_FILENAME = "openapi.json"
OPENAPI_UI = "swagger"


RE_PARSE_RULE = re.compile(
    r"""
    (?P<static>[^<]*)                           # static rule data
    <
    (?:
        (?P<converter>[a-zA-Z_][a-zA-Z0-9_]*)   # converter name
        (?:\((?P<args>.*?)\))?                  # converter arguments
        \:                                      # variable delimiter
    )?
    (?P<variable>[a-zA-Z_][a-zA-Z0-9_]*)        # variable name
    >
    """,
    re.VERBOSE,
)


def parse_rule(rule):
    """
    Parse a rule and return it as generator. Each iteration yields tuples in the form
    ``(converter, arguments, variable)``. If the converter is `None` it's a static url part, otherwise it's a dynamic
    one.
    Note: This originally lived in werkzeug.routing.parse_rule until it was removed in werkzeug 2.2.0.
    """
    pos = 0
    end = len(rule)
    do_match = RE_PARSE_RULE.match
    used_names = set()
    while pos < end:
        m = do_match(rule, pos)
        if m is None:
            break
        data = m.groupdict()
        if data["static"]:
            yield None, None, data["static"]
        variable = data["variable"]
        converter = data["converter"] or "default"
        if variable in used_names:
            raise ValueError(f"variable name {variable!r} used twice.")
        used_names.add(variable)
        yield converter, data["args"] or None, variable
        pos = m.end()
    if pos < end:
        remaining = rule[pos:]
        if ">" in remaining or "<" in remaining:
            raise ValueError(f"malformed url rule: {rule!r}")
        yield None, None, remaining


def add_openapi_spec(
    app: Flask,
    endpoint: str = OPENAPI_ENDPOINT,
    url_prefix: Optional[str] = OPENAPI_URL_PREFIX,
    mode: str = OPENAPI_MODE,
    openapi_version: str = OPENAPI_VERSION,
    openapi_info: dict = OPENAPI_INFO,
    extra_props: dict = {},
):
    assert isinstance(app, Flask)
    assert mode in {"normal", "greedy", "strict"}

    if not hasattr(add_openapi_spec, "openapi"):
        add_openapi_spec.openapi = OpenAPI(
            app,
            endpoint=endpoint,
            url_prefix=url_prefix,
            mode=mode,
            openapi_version=openapi_version,
            openapi_info=openapi_info,
            extra_props=extra_props,
        )
    openapi = add_openapi_spec.openapi
    openapi.extra_props = extra_props

    return openapi.spec


class APIView(MethodView):
    def __init__(self, *args, **kwargs):
        view_args = kwargs.pop("view_args", {})
        self.ui = view_args.get("ui")
        self.filename = view_args.get("filename")
        super().__init__(*args, **kwargs)

    def get(self):
        assert self.ui in {"redoc", "swagger"}
        ui_file = f"{self.ui}.html"
        return render_template(ui_file, spec_url=self.filename)


class APIError:
    def __init__(self, code: int, msg: str) -> None:
        self.code = code
        self.msg = msg

    def __repr__(self) -> str:
        return f"{self.code} {self.msg}"


class OpenAPI:
    _models = {}

    def __init__(
        self,
        app: Flask,
        endpoint: str = OPENAPI_ENDPOINT,
        url_prefix: Optional[str] = OPENAPI_URL_PREFIX,
        mode: str = OPENAPI_MODE,
        openapi_version: str = OPENAPI_VERSION,
        openapi_info: dict = OPENAPI_INFO,
        extra_props: dict = {},
    ) -> None:
        assert isinstance(app, Flask)

        self.app = app
        self.endpoint: str = endpoint
        self.url_prefix: Optional[str] = url_prefix
        self.mode: str = mode
        self.openapi_version: str = openapi_version
        self.info: dict = openapi_info
        self.extra_props: dict = extra_props

        self._spec = None

    @property
    def spec(self):
        if self._spec is None:
            self._spec = self.generate_spec()
        return self._spec

    def _bypass(self, func) -> bool:
        if self.mode == "greedy":
            return False
        elif self.mode == "strict":
            if getattr(func, "_openapi", None) == self.__class__:
                return False
            return True
        else:
            decorator = getattr(func, "_openapi", None)
            if decorator and decorator != self.__class__:
                return True
            return False

    def generate_spec(self):
        """
        generate OpenAPI spec JSON file
        """

        routes = {}
        tags = {}

        for rule in self.app.url_map.iter_rules():
            if str(rule).startswith(
                (f"{self.url_prefix or ''}{self.endpoint}", "/static")
            ):
                continue

            if "resources" in str(rule):
                pass

            func = self.app.view_functions[rule.endpoint]
            path, parameters = parse_url(str(rule))

            # bypass the function decorated by others
            if self._bypass(func):
                continue

            # multiple methods (with different func) may bond to the same path
            if path not in routes:
                routes[path] = {}

            for method in rule.methods:
                if method in ["HEAD", "OPTIONS"]:
                    continue

                if hasattr(func, "tags"):
                    for tag in func.tags:
                        if tag not in tags:
                            tags[tag] = {"name": tag}

                summary, desc = get_summary_desc(func)
                spec = {
                    "summary": summary or func.__name__.capitalize(),
                    "description": desc or "",
                    "operationID": func.__name__ + "__" + method.lower(),
                    "tags": getattr(func, "tags", []),
                }

                params = parameters[:]
                view_class = getattr(func, "view_class", None)

                if hasattr(view_class, method.lower()):
                    view_func = getattr(view_class, method.lower())
                    if hasattr(view_func, "body"):
                        spec["requestBody"] = {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": f"#/components/schemas/{view_func.body}"}
                                }
                            }
                        }

                    if hasattr(view_func, "form"):
                        spec["requestBody"] = {
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": f"#/components/schemas/{view_func.form}"}
                                }
                            }
                        }

                    if hasattr(view_func, "query"):
                        params.append(
                            {
                                "name": view_func.query,
                                "in": "query",
                                "required": True,
                                "schema": {
                                    "$ref": f"#/components/schemas/{view_func.query}",
                                },
                            }
                        )
                spec["parameters"] = params

                spec["responses"] = {}
                has_2xx = False
                if hasattr(func, "exceptions"):
                    for code, msg in func.exceptions.items():
                        if code.startswith("2"):
                            has_2xx = True
                        spec["responses"][code] = {
                            "description": msg,
                        }

                if hasattr(func, "response"):
                    spec["responses"]["200"] = {
                        "description": "Successful Response",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": f"#/components/schemas/{func.response}"
                                }
                            }
                        },
                    }
                elif not has_2xx:
                    spec["responses"]["200"] = {"description": "Successful Response"}

                if any(
                    [hasattr(func, schema) for schema in ("query", "body", "form", "response")]
                ):
                    spec["responses"]["400"] = {
                        "description": "Validation Error",
                    }

                routes[path][method.lower()] = spec

        definitions = {}
        for _, schema in self._models.items():
            if "definitions" in schema:
                for key, value in schema["definitions"].items():
                    definitions[key] = value
                del schema["definitions"]

        data = {
            "openapi": self.openapi_version,
            "info": self.info,
            "tags": list(tags.values()),
            "paths": {**routes},
            "components": {
                "schemas": {name: schema for name, schema in self._models.items()},
            },
            "definitions": definitions,
        }

        merge_dicts(data, self.extra_props)

        return data

    @classmethod
    def add_model(cls, model):
        cls._models[model.__name__] = model.schema()


def get_summary_desc(func):
    """
    get summary, description from `func.__doc__`

    Summary and description are split by '\n\n'. If only one is provided,
    it will be used as summary.
    """
    doc = inspect.getdoc(func)
    if not doc:
        return None, None
    doc = doc.split("\n\n", 1)
    if len(doc) == 1:
        return doc[0], None
    return doc


def get_converter_schema(converter: str, *args, **kwargs):
    """
    get json schema for parameters in url based on following converters
    https://werkzeug.palletsprojects.com/en/0.15.x/routing/#builtin-converter
    """
    if converter == "any":
        return {"type": "array", "items": {"type": "string", "enum": args}}
    elif converter == "int":
        return {
            "type": "integer",
            "format": "int32",
            **{
                f"{prop}imum": kwargs[prop] for prop in ["min", "max"] if prop in kwargs
            },
        }
    elif converter == "float":
        return {"type": "number", "format": "float"}
    elif converter == "uuid":
        return {"type": "string", "format": "uuid"}
    elif converter == "path":
        return {"type": "string", "format": "path"}
    elif converter == "string":
        return {
            "type": "string",
            **{
                prop: kwargs[prop]
                for prop in ["length", "maxLength", "minLength"]
                if prop in kwargs
            },
        }
    else:
        return {"type": "string"}


def parse_url(path: str):
    """
    Parsing Flask route url to get the normal url path and parameter type.

    Based on Werkzeug_ builtin converters.

    .. _werkzeug: https://werkzeug.palletsprojects.com/en/0.15.x/routing/#builtin-converters
    """
    subs = []
    parameters = []

    for converter, arguments, variable in parse_rule(path):
        if converter is None:
            subs.append(variable)
            continue
        subs.append(f"{{{variable}}}")

        args, kwargs = [], {}

        if arguments:
            args, kwargs = parse_converter_args(arguments)

        schema = get_converter_schema(converter, *args, **kwargs)

        parameters.append(
            {
                "name": variable,
                "in": "path",
                "required": True,
                "schema": schema,
            }
        )

    return "".join(subs), parameters


def merge_dicts(d1, d2):
    """
    Merge dictionary `d2` into `d1` and return the `d1`.

    If `d2` has nested dictionaries which also exists in `d1`, the nested dictionaries
    will be merged recursively instead of replacing them.

    For example:
    merge_dicts({"c": {"a":1}}, {"c":{"b":2}}) => {"c":{"a": 1, "b": 2}}

    """
    for k, v in d1.items():
        if k in d2:
            v2 = d2.pop(k)
            if isinstance(v, dict):
                if isinstance(v2, dict):
                    merge_dicts(v, v2)
                else:
                    d1[k] = v2
            else:
                d1[k] = v2
    d1.update(d2)
    return d1

def openapi_docs(
    response: Optional[Type[BaseModel]] = None,
    exceptions: List[APIError] = [],
    tags: List[str] = [],
):
    def decorate(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            res = func(*args, **kwargs)
            return res

        query = func.__annotations__.get("query") or getattr(func, "_query", None)
        body = func.__annotations__.get("body") or getattr(func, "_body", None)
        form = func.__annotations__.get("form") or getattr(func, "_form", None)

        # register schemas to this function
        for model, name in zip((query, body, form, response), ("query", "body", "form", "response")):
            if model:
                assert issubclass(model, BaseModel)
                OpenAPI.add_model(model)
                setattr(wrapper, name, model.__name__)

        # register exceptions
        api_errs = {}
        for e in exceptions:
            assert isinstance(e, APIError)
            api_errs[str(e.code)] = e.msg
        if api_errs:
            setattr(wrapper, "exceptions", api_errs)

        # register tags
        if tags:
            setattr(wrapper, "tags", tags)

        # register OpenAPI class
        setattr(wrapper, "_openapi", OpenAPI)

        return wrapper

    return decorate

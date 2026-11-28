"""The server side of the CAD engine (plan `.10x/plans/2026-11-28-cad-board-slice-1.md`, Task 3):
the runner client, the step check and the picture check. The runner is `tests/fakes/cad_runner`;
no database. Every drawing is fictional.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import time
import tracemalloc
from collections.abc import Callable
from typing import Any

import pytest

from jarvis_server import contracts as c
from jarvis_server.core import cad_engine
from jarvis_server.core.cad_engine import (
    BadPicture,
    BadStep,
    EngineDown,
    EngineRefused,
    Replay,
    Step,
    check_svg,
    step_from,
)

from .fakes import cad_runner
from .fakes.cad_runner import (
    CLOSE_EARLY,
    NEVER_ANSWER,
    NOT_JSON,
    OVERLONG,
    PNG,
    SVG,
    FakeRunner,
    HoldOpen,
    Raw,
    Trickle,
    b64,
    failure,
    good_replay,
)

ROOM = {"command": "rectang", "params": {"corner1": [0, 0], "corner2": [4000, 3000]}}
LABEL = {"command": "text", "params": {"insert": [2000, 1500], "text": "Living room"}}


def _steps() -> list[Step]:
    return [
        step_from({"label": "Outer walls", "commands": [ROOM]}),
        step_from({"label": "Room names", "commands": [LABEL, LABEL]}),
    ]


def _answer(edit: Callable[[dict[str, Any]], Any]) -> Callable[..., Any]:
    """A good answer to the request, changed by `edit` (which changes it in place)."""

    def make(request: dict[str, Any]) -> dict[str, Any]:
        a = good_replay(request)
        edit(a)
        return a

    return make


# --- the commands file -----------------------------------------------------------------------


def test_allowed_is_the_commands_file() -> None:
    assert len(cad_engine.ALLOWED) == 37
    assert {"line", "rectang", "text", "dimlinear", "layer.new"} <= cad_engine.ALLOWED
    assert cad_engine.COMMANDS_FILE.name == "cad_commands.txt"
    assert cad_engine.COMMANDS_FILE.parent.name == "config"
    assert (cad_engine.STEP_MAX, cad_engine.RECIPE_MAX) == (400, 2000)


# --- step_from ---------------------------------------------------------------------------------


def test_step_from_keeps_label_and_commands() -> None:
    step = step_from({"label": "Outer walls", "commands": [ROOM, LABEL]})
    assert step == Step("Outer walls", (ROOM, LABEL))
    assert step.commands[0] is not ROOM  # a copy: the caller's dict can't change the step


def test_step_from_refuses_a_command_outside_the_list() -> None:
    with pytest.raises(BadStep, match="allowed"):
        step_from({"label": "Walls", "commands": [{"command": "shell", "params": {}}]})


def test_step_from_refuses_401_commands() -> None:
    step_from({"label": "Many lines", "commands": [ROOM] * 400})
    with pytest.raises(BadStep, match="400"):
        step_from({"label": "Many lines", "commands": [ROOM] * 401})


@pytest.mark.parametrize(
    "args",
    [
        None,
        [],
        {"commands": [ROOM]},
        {"label": "", "commands": [ROOM]},
        {"label": "   ", "commands": [ROOM]},
        {"label": "two\nlines", "commands": [ROOM]},
        {"label": "sep\u2028arated", "commands": [ROOM]},
        {"label": "x" * 81, "commands": [ROOM]},
        {"label": 7, "commands": [ROOM]},
        {"label": "Walls"},
        {"label": "Walls", "commands": []},
        {"label": "Walls", "commands": ROOM},
        {"label": "Walls", "commands": [ROOM], "extra": 1},
        {"label": "Walls", "commands": ["line"]},
        {"label": "Walls", "commands": [{"command": "line"}]},
        {"label": "Walls", "commands": [{"command": "line", "params": None}]},
        {"label": "Walls", "commands": [{"command": "line", "params": [1, 2]}]},
        {"label": "Walls", "commands": [{"command": "line", "params": "{}"}]},
        {"label": "Walls", "commands": [{"command": 3, "params": {}}]},
        {"label": "Walls", "commands": [{"command": "line", "params": {}, "x": 1}]},
        {"label": "Walls", "commands": [{"command": "line", "params": {"a": float("nan")}}]},
        {"label": "Walls", "commands": [{"command": "line", "params": {"a": "\ud800"}}]},
        {"label": "Walls", "commands": [{"command": "line", "params": {"a": {1, 2}}}]},
    ],
)
def test_step_from_refuses_what_the_runner_would_not_take(args: Any) -> None:
    with pytest.raises(BadStep):
        step_from(args)


def test_step_from_bounds_the_size_and_depth_of_params() -> None:
    big = {"command": "text", "params": {"text": "a" * 100_000}}
    step_from({"label": "Notes", "commands": [big]})
    with pytest.raises(BadStep, match="200,000"):
        step_from({"label": "Notes", "commands": [big, big]})
    deep: Any = 1
    for _ in range(40):
        deep = {"a": deep}
    with pytest.raises(BadStep, match="deep"):
        step_from({"label": "Nested", "commands": [{"command": "line", "params": deep}]})


# --- replay ------------------------------------------------------------------------------------


def test_replay_parses_a_good_answer() -> None:
    with FakeRunner() as runner:
        got = cad_engine.replay(_steps(), picture=True, address=runner.address)
    assert isinstance(got, Replay)
    assert [(r.step, r.n, r.command, r.ok) for r in got.results] == [
        (1, 1, "rectang", True),
        (2, 1, "text", True),
        (2, 2, "text", True),
    ]
    assert got.failed == 0
    assert got.counts == {"Rectang": 1, "Text": 2} and got.items == 3
    assert got.extents == (420.0, 297.0)
    assert got.texts == ("Living room", "Living room")
    assert got.png == PNG and got.svg == SVG
    assert (got.engine, got.engine_version) == ("cadcraft", "0.12.0")
    sent = runner.requests[0]
    assert sent == {
        "v": 1,
        "op": "replay",
        "picture": True,
        "steps": [
            {"label": "Outer walls", "commands": [ROOM]},
            {"label": "Room names", "commands": [LABEL, LABEL]},
        ],
    }


def test_replay_without_a_picture_has_no_svg() -> None:
    with FakeRunner() as runner:
        got = cad_engine.replay(_steps(), picture=False, address=runner.address)
    assert got.svg is None and got.png == PNG
    assert runner.requests[0]["picture"] is False


def test_a_failed_command_is_reported() -> None:
    def one_failed(request: dict[str, Any]) -> dict[str, Any]:
        return good_replay(request, failed=[(2, 2)])

    with FakeRunner([one_failed]) as runner:
        got = cad_engine.replay(_steps(), picture=True, address=runner.address)
    assert got.failed == 1 and got.svg is None
    assert [r.ok for r in got.results] == [True, True, False]
    assert got.results[2].text == "Error: text failed"


def test_catalogue() -> None:
    with FakeRunner() as runner:
        got = cad_engine.catalogue(address=runner.address)
    assert set(got) == cad_engine.ALLOWED
    assert got["line"] == "line parameters (fictional note)"
    assert runner.requests == [{"v": 1, "op": "catalogue"}]


@pytest.mark.parametrize(
    "edit",
    [
        lambda a: a.update(v=2),
        lambda a: a.update(ok="yes"),
        lambda a: a.update(engine_version="0.12 0"),
        lambda a: a.update(commands={"shell": "run anything"}),
        lambda a: a.update(commands={"line": "two\nlines"}),
        lambda a: a.update(commands={"line": "x" * 2001}),
        lambda a: a.update(commands=["line"]),
        lambda a: a.update(extra=1),
    ],
)
def test_a_bad_catalogue_is_engine_down(edit: Callable[[dict[str, Any]], Any]) -> None:
    def make(_request: Any) -> dict[str, Any]:
        a = cad_runner.catalogue_answer()
        edit(a)
        return a

    with FakeRunner([make]) as runner, pytest.raises(EngineDown):
        cad_engine.catalogue(address=runner.address)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_no_listener_is_engine_down() -> None:
    with pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=f"127.0.0.1:{_free_port()}")
    with pytest.raises(EngineDown):
        cad_engine.catalogue(address=f"127.0.0.1:{_free_port()}")


def test_the_address_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    with FakeRunner() as runner:
        monkeypatch.setenv("JARVIS_CAD_ADDR", runner.address)
        cad_engine.replay(_steps(), picture=False)
    assert len(runner.requests) == 1
    monkeypatch.delenv("JARVIS_CAD_ADDR")
    assert cad_engine.address() == "cad:7411"


@pytest.mark.parametrize(
    "bad",
    [
        "cad", "cad:", ":7411", "cad:port", "cad:0", "cad:70000", "", "cad:\u0667",
        "a" * 300 + ":7411", "x" * 64 + ".example:7411", "a..b:7411", "\udcff:7411",
    ],
)  # fmt: skip
def test_a_malformed_address_is_engine_down(bad: str) -> None:
    with pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=bad)


@pytest.mark.parametrize("code", ["busy", "engine_failed", "bad_request", "something_new"])
def test_busy_and_engine_failed_are_engine_down(code: str) -> None:
    with FakeRunner([failure(code), failure(code)]) as runner:
        with pytest.raises(EngineDown):
            cad_engine.replay(_steps(), picture=False, address=runner.address)
        with pytest.raises(EngineDown):
            cad_engine.catalogue(address=runner.address)


@pytest.mark.parametrize("code", ["too_large", "too_many_commands", "not_allowed", "timeout"])
def test_too_large_is_engine_refused(code: str) -> None:
    with FakeRunner([failure(code)]) as runner, pytest.raises(EngineRefused) as caught:
        cad_engine.replay(_steps(), picture=True, address=runner.address)
    assert caught.value.code == code


@pytest.mark.parametrize("code", ["too_large", "too_many_commands", "not_allowed", "timeout"])
def test_every_catalogue_failure_is_engine_down(code: str) -> None:
    with FakeRunner([failure(code)]) as runner, pytest.raises(EngineDown):
        cad_engine.catalogue(address=runner.address)


@pytest.mark.parametrize(
    "answer",
    [
        {"v": 1, "ok": False},
        {"v": 1, "ok": False, "error": 3},
        {"v": 1, "ok": False, "error": "busy", "more": "x"},
        {"v": True, "ok": False, "error": "too_large"},
        [],
        "ok",
        Raw(b"null\n"),  # (None in a script means the default answer)
    ],
)
def test_a_malformed_failure_is_engine_down(answer: Any) -> None:
    with FakeRunner([answer]) as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=runner.address)


def _results(a: dict[str, Any]) -> list[dict[str, Any]]:
    return a["results"]  # type: ignore[no-any-return]


WRONG: list[Callable[[dict[str, Any]], Any]] = [
    lambda a: a.update(v=2),
    lambda a: a.update(v=True),
    lambda a: a.update(ok=1),
    lambda a: a.pop("texts"),
    lambda a: a.update(surprise=None),
    lambda a: a.update(engine="CADcraft"),
    lambda a: a.update(engine="x" * 31),
    lambda a: a.update(engine_version="0.12.0\n"),
    lambda a: a.update(engine_version="v 1"),
    lambda a: a.update(engine_version=""),
    lambda a: a.update(results="none"),
    lambda a: _results(a).pop(),
    lambda a: _results(a).append(dict(_results(a)[0])),
    lambda a: _results(a).reverse(),
    lambda a: _results(a)[0].update(step="1"),
    lambda a: _results(a)[0].update(n=True),
    lambda a: _results(a)[0].update(command="line"),
    lambda a: _results(a)[0].update(ok="true"),
    lambda a: _results(a)[0].update(text="x" * 301),
    lambda a: _results(a)[0].update(text="two\nlines"),
    lambda a: _results(a)[0].update(text="bell \x07"),
    lambda a: _results(a)[0].update(text=None),
    lambda a: _results(a)[0].update(extra=1),
    lambda a: a.update(failed=1),
    lambda a: a.update(failed=-1),
    lambda a: a.update(failed=False),
    lambda a: a.update(counts=[["Line", 1]]),
    lambda a: a.update(counts={"Line": -1}, items=-1),
    lambda a: a.update(counts={"Line": 1.5}, items=1.5),
    lambda a: a.update(counts={"Line": True}, items=1),
    lambda a: a.update(counts={"": 1}, items=1),
    lambda a: a.update(counts={"L" * 41: 1}, items=1),
    lambda a: a.update(counts={f"K{i}": 1 for i in range(65)}, items=65),
    lambda a: a.update(items=4),
    lambda a: a.update(counts={"Line": 100_001}, items=100_001),
    lambda a: a.update(items="3"),
    lambda a: a.update(extents=[1.0]),
    lambda a: a.update(extents=[1.0, 2.0, 3.0]),
    lambda a: a.update(extents=[-1.0, 2.0]),
    lambda a: a.update(extents=[1.0, "2"]),
    lambda a: a.update(extents=[True, 2.0]),
    lambda a: a.update(extents={"w": 1, "h": 2}),
    lambda a: a.update(extents=[1e400, 1.0]),
    lambda a: a.update(texts=["x"] * 81),
    lambda a: a.update(texts=["x" * 81]),
    lambda a: a.update(texts=[3]),
    lambda a: a.update(texts=["two\nlines"]),
    lambda a: a.update(texts="Living room"),
    lambda a: a.update(png_b64=b64(b"GIF89a fictional")),
    lambda a: a.update(png_b64="not base64!"),
    lambda a: a.update(png_b64=b64(PNG)[:-2]),
    lambda a: a.update(png_b64=b64(PNG + b"\0" * (2 * 1024 * 1024))),
    lambda a: a.update(png_b64=3),
    lambda a: a.update(svg_b64=None),
    lambda a: a.update(svg_b64="!!!!"),
    lambda a: a.update(svg_b64=b64(b"<svg" + b" " * 2_000_000 + b"/>")),
    lambda a: a.update(svg_b64=[]),
]


@pytest.mark.parametrize("edit", WRONG)
def test_an_answer_with_wrong_types_is_engine_down(edit: Callable[[dict[str, Any]], Any]) -> None:
    with FakeRunner([_answer(edit)]) as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=True, address=runner.address)


def test_an_svg_that_was_not_asked_for_is_engine_down() -> None:
    edit = _answer(lambda a: a.update(svg_b64=b64(SVG)))
    with FakeRunner([edit]) as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=runner.address)


def test_an_svg_with_failed_commands_is_engine_down() -> None:
    def failed_with_svg(request: dict[str, Any]) -> dict[str, Any]:
        a = good_replay(request, failed=[(1, 1)])
        a["svg_b64"] = b64(SVG)
        return a

    with FakeRunner([failed_with_svg]) as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=True, address=runner.address)


def test_null_extents_and_png_are_fine() -> None:
    edit = _answer(lambda a: a.update(extents=None, png_b64=None, texts=[]))
    with FakeRunner([edit]) as runner:
        got = cad_engine.replay(_steps(), picture=False, address=runner.address)
    assert (got.extents, got.png, got.texts) == (None, None, ())


@pytest.mark.parametrize(
    "line",
    [
        b'{"v":1,"ok":false,"error":"too_large"}\n{"v":1}\n',  # a second line
        b'{"v":1,"ok":false,"error":"too_large"}',  # no newline before the close
        b'{"v":1,"ok":false,"error":"too_large","error":"busy"}\n',  # a repeated key
        b'{"v":1,"ok":true,"extents":NaN}\n',
        b"\xff\xfe{}\n",
        b'{"v":1,"ok":false,"error":"too_l\xe9rge"}\n',  # not UTF-8
        b"[" * 100_000 + b"\n",
        b"\n",
    ],
)
def test_a_line_that_is_not_one_good_json_object_is_engine_down(line: bytes) -> None:
    with FakeRunner([Raw(line)]) as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=runner.address)


@pytest.mark.parametrize("misbehave", [CLOSE_EARLY, OVERLONG, NOT_JSON])
def test_a_runner_that_misbehaves_is_engine_down(misbehave: Any) -> None:
    with FakeRunner([misbehave]) as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=runner.address)


def test_a_runner_that_never_answers_is_engine_down_at_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cad_engine, "EXCHANGE_TIMEOUT", 0.5)
    with FakeRunner([NEVER_ANSWER]) as runner:
        started = time.monotonic()
        with pytest.raises(EngineDown):
            cad_engine.replay(_steps(), picture=False, address=runner.address)
    assert time.monotonic() - started < 3


def test_a_trickling_runner_cannot_reset_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each byte comes well inside the deadline; the whole answer would take 4 s."""
    monkeypatch.setattr(cad_engine, "EXCHANGE_TIMEOUT", 0.8)
    line = json.dumps(failure("too_large")).encode() + b"\n"
    with FakeRunner([Trickle(line, interval=4 / len(line))]) as runner:
        started = time.monotonic()
        with pytest.raises(EngineDown):
            cad_engine.replay(_steps(), picture=False, address=runner.address)
    assert time.monotonic() - started < 2.5


def test_the_limits_are_the_plans() -> None:
    assert cad_engine.CONNECT_TIMEOUT == 3
    assert cad_engine.EXCHANGE_TIMEOUT == 20
    assert cad_engine.ANSWER_MAX_BYTES == 8 * 1024 * 1024
    assert cad_engine.REQUEST_MAX_BYTES == 1_000_000


def test_a_recipe_over_2000_commands_is_refused_before_connecting() -> None:
    steps = [step_from({"label": f"Lines {i}", "commands": [ROOM] * 400}) for i in range(5)]
    steps.append(step_from({"label": "One more", "commands": [ROOM]}))
    with FakeRunner() as runner:
        with pytest.raises(EngineRefused) as caught:
            cad_engine.replay(steps, picture=False, address=runner.address)
        assert caught.value.code == "too_many_commands"
        cad_engine.replay(steps[:5], picture=False, address=runner.address)  # 2,000 is fine
    assert runner.connections == 1


def test_a_request_line_over_1000000_bytes_is_refused_before_connecting() -> None:
    note = {"command": "text", "params": {"text": "a" * 99_000}}
    steps = [step_from({"label": f"Notes {i}", "commands": [note, note]}) for i in range(6)]
    with FakeRunner() as runner:
        with pytest.raises(EngineRefused) as caught:
            cad_engine.replay(steps, picture=False, address=runner.address)
        assert caught.value.code == "request_too_large"
    assert runner.connections == 0


def test_the_client_logs_no_content(caplog: pytest.LogCaptureFixture) -> None:
    secret_label = "Fictional secret room"
    secret_text = "Fictional answer text"

    def answer(request: dict[str, Any]) -> dict[str, Any]:
        a = good_replay(request)
        a["results"][0]["text"] = secret_text
        return a

    steps = [step_from({"label": secret_label, "commands": [ROOM]})]
    caplog.set_level(logging.DEBUG)
    with FakeRunner([answer, failure("too_large"), Raw(b"garbage " + secret_text.encode())]) as r:
        cad_engine.replay(steps, picture=False, address=r.address)
        with pytest.raises(EngineRefused):
            cad_engine.replay(steps, picture=False, address=r.address)
        with pytest.raises(EngineDown):
            cad_engine.replay(steps, picture=False, address=r.address)
    logged = caplog.text
    assert logged  # it does log: codes and sizes
    assert secret_label not in logged and secret_text not in logged and "Living" not in logged


def test_engine_errors_carry_no_answer_content() -> None:
    words = Raw(b'{"v":1,"ok":false,"error":"Fictional words"}\n')
    with FakeRunner([words]) as runner, pytest.raises(EngineDown) as caught:
        cad_engine.replay(_steps(), picture=False, address=runner.address)
    assert "Fictional" not in str(caught.value)


# --- check_svg (Review Focus 2) -----------------------------------------------------------------

NS = 'xmlns="http://www.w3.org/2000/svg"'


def _svg(inner: str, *, root_attrs: str = "", decl: bool = True) -> bytes:
    head = '<?xml version="1.0" encoding="UTF-8"?>\n' if decl else ""
    return f'{head}<svg {NS} width="10" height="10"{root_attrs}>{inner}</svg>'.encode()


def test_check_svg_accepts_the_engines_own_shapes() -> None:
    check_svg(SVG)
    check_svg(_svg('<rect width="1" height="1" fill="#fff"/>', decl=False))
    check_svg(_svg('<path d="M 0 0 L 1 1"/>').replace(b'encoding="UTF-8"', b'encoding="utf-8"'))
    check_svg(b"<?xml version='1.0'?>" + _svg("", decl=False))
    check_svg(
        _svg(
            '<g transform="translate(1 1)" opacity="0.5"><line x1="0" y1="0" x2="1" y2="1"'
            ' stroke-dasharray="1 2"/><circle cx="1" cy="1" r="1"/>'
            '<ellipse cx="1" cy="1" rx="1" ry="2"/><polygon points="0,0 1,1 1,0"/></g>'
        )
    )


def test_check_svg_accepts_a_large_drawing_of_many_shapes() -> None:
    shape = b'<path d="M 0 0 L 10 10 L 20 0" fill="none" stroke="#000" stroke-width="0.35"/>\n'
    body = shape * ((c.BOARD_PICTURE_MAX_BYTES - 400) // len(shape))
    svg = _svg("").replace(b"</svg>", body + b"</svg>")
    assert len(svg) <= c.BOARD_PICTURE_MAX_BYTES
    check_svg(svg)


REFUSED = {
    "script element": _svg("<script>alert(1)</script>"),
    "foreignObject": _svg("<foreignObject><div/></foreignObject>"),
    "image": _svg('<image width="1" height="1"/>'),
    "use": _svg('<use x="1"/>'),
    "text element": _svg("<text>Living room</text>"),
    "style element": _svg("<style>rect{fill:red}</style>"),
    "onload attribute": _svg("", root_attrs=' onload="alert(1)"'),
    "style attribute": _svg('<rect width="1" height="1" style="fill:red"/>'),
    "href": _svg('<a href="https://example.invalid/"><rect width="1" height="1"/></a>'),
    "href on an allowed element": _svg('<rect width="1" height="1" href="#x"/>'),
    "xlink:href": _svg(
        '<rect width="1" height="1" xlink:href="#x"/>',
        root_attrs=' xmlns:xlink="http://www.w3.org/1999/xlink"',
    ),
    "xml:base": _svg('<rect width="1" height="1" xml:base="https://example.invalid/"/>'),
    "another namespace declared": _svg(
        '<rect width="1" height="1"/>', root_attrs=' xmlns:x="urn:fictional"'
    ),
    "an element in another namespace": _svg('<path xmlns="urn:fictional" d="M 0 0"/>'),
    "root not svg": b'<path xmlns="http://www.w3.org/2000/svg" d="M 0 0"/>',
    "root svg in no namespace": b'<svg width="1" height="1"><path d="M 0 0"/></svg>',
    "root svg in another namespace": b'<svg xmlns="urn:fictional"><path d="M 0 0"/></svg>',
    "DOCTYPE": b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "x.dtd">' + _svg("", decl=False),
    "entity": (
        b'<!DOCTYPE svg [<!ENTITY a "aaaaaaaaaa">]>'
        + _svg('<rect width="1" height="1" fill="&a;"/>', decl=False)
    ),
    "external entity": (
        b'<!DOCTYPE svg [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        + _svg('<rect width="1" height="1" fill="&e;"/>', decl=False)
    ),
    "CDATA": _svg("<![CDATA[x]]>"),
    "comment": _svg("<!-- a fictional note -->"),
    "processing instruction": _svg('<?xml-stylesheet href="x.css"?>'),
    "XML declaration not first": b"\n" + _svg(""),
    "XML declaration of another encoding": _svg("").replace(b"UTF-8", b"ISO-8859-1"),
    "a second XML declaration": b'<?xml version="1.0"?>' + _svg(""),
    "character reference": _svg('<rect width="1" height="1" fill="&#x72;ed"/>'),
    "named entity": _svg('<rect width="1" height="1" fill="&amp;"/>'),
    "url( in fill": _svg('<rect width="1" height="1" fill="url(#gradient)"/>'),
    "URL ( with a space and capitals": _svg('<rect width="1" height="1" fill="URL (#g)"/>'),
    "javascript: in a value": _svg('<path d="javascript:alert(1)"/>'),
    "data: in a value": _svg('<rect width="1" height="1" fill="data:image/png"/>'),
    "backslash in a value": _svg('<rect width="1" height="1" fill="\\72 ed"/>'),
    "a short attribute too long": _svg('<rect width="1" height="1" fill="' + "a" * 201 + '"/>'),
    "a path too long": _svg('<path d="M' + " 0" * 500_001 + '"/>'),
    "text content": _svg('<rect width="1" height="1">Living room</rect>'),
    "text between elements": _svg('<rect width="1" height="1"/>Living room'),
    "text in the root": _svg("Living room"),
    "too deep": _svg("<g>" * 33 + "</g>" * 33),
    "a 2,000,001-byte file": _svg("").replace(b"</svg>", b" " * 2_000_001 + b"</svg>"),
    "bad XML": _svg('<rect width="1"'),
    "unbound prefix": _svg("<x:rect/>"),
    "empty": b"",
    "UTF-8 BOM": b"\xef\xbb\xbf" + _svg(""),
    "UTF-16": _svg("").decode().encode("utf-16"),
    "UTF-16 without a BOM": _svg("").decode().encode("utf-16-le"),
    "UTF-32": _svg("").decode().encode("utf-32"),
    "not UTF-8": _svg("").replace(b'width="10"', b'width="1\xe9"'),
    "NUL": _svg("\x00"),
    "two roots": _svg("") + _svg("", decl=False),
}


@pytest.mark.parametrize("svg", REFUSED.values(), ids=REFUSED.keys())
def test_check_svg_refuses(svg: bytes) -> None:
    with pytest.raises(BadPicture):
        check_svg(svg)


def test_check_svg_counts_elements(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cad_engine.SVG_ELEMENTS_MAX == 400_000
    assert cad_engine.SVG_DEPTH_MAX == 32
    monkeypatch.setattr(cad_engine, "SVG_ELEMENTS_MAX", 10)
    check_svg(_svg('<rect width="1" height="1"/>' * 9))
    with pytest.raises(BadPicture, match=r"^count$"):
        check_svg(_svg('<rect width="1" height="1"/>' * 10))


def test_check_svg_is_fine_at_depth_32_and_refuses_33() -> None:
    check_svg(_svg("<g>" * 31 + "</g>" * 31))  # the root is level 1
    with pytest.raises(BadPicture, match=r"^depth$"):
        check_svg(_svg("<g>" * 32 + "</g>" * 32))


def test_check_svg_refuses_what_is_not_bytes() -> None:
    with pytest.raises(BadPicture):
        check_svg(SVG.decode())  # type: ignore[arg-type]


def test_the_fake_runners_picture_passes_and_decodes() -> None:
    check_svg(base64.b64decode(good_replay({"steps": [], "picture": True})["svg_b64"]))


# --- fixes from the independent review --------------------------------------------------------

SURROGATE = "\udc80"


@pytest.mark.parametrize(
    "edit",
    [
        lambda a: _results(a)[0].update(text=f"{SURROGATE} done"),
        lambda a: a.update(texts=[f"Living {SURROGATE}"]),
        lambda a: a.update(counts={f"Line{SURROGATE}": 3}),
    ],
    ids=["result text", "texts", "counts name"],
)
def test_a_lone_surrogate_in_a_replay_answer_is_engine_down(
    edit: Callable[[dict[str, Any]], Any],
) -> None:
    with FakeRunner([_answer(edit)]) as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=runner.address)


def test_a_lone_surrogate_in_a_catalogue_note_is_engine_down() -> None:
    a = cad_runner.catalogue_answer()
    a["commands"]["line"] = f"start, end {SURROGATE}"
    with FakeRunner([a]) as runner, pytest.raises(EngineDown):
        cad_engine.catalogue(address=runner.address)


def test_every_string_of_a_good_answer_encodes_as_utf8() -> None:
    def answer(request: dict[str, Any]) -> dict[str, Any]:
        a = good_replay(request)
        a["results"][0]["text"] = "Wall drawn: ok é中"  # fine non-ASCII
        return a

    with FakeRunner([answer]) as runner:
        got = cad_engine.replay(_steps(), picture=False, address=runner.address)
    for text in [r.text for r in got.results] + list(got.texts) + list(got.counts):
        text.encode("utf-8")


def test_step_from_refuses_lone_surrogates() -> None:
    with pytest.raises(BadStep):
        step_from({"label": f"Walls {SURROGATE}", "commands": [ROOM]})
    with pytest.raises(BadStep):
        step_from({"label": "Walls", "commands": [{"command": "text",
                                                   "params": {SURROGATE: 1}}]})  # fmt: skip


@pytest.mark.parametrize(
    "edit",
    [
        lambda a: a.update(failed=1),  # every result ok, one counted as failed
        lambda a: _results(a)[1].update(ok=False),  # one failed, none counted
        lambda a: a.update(failed=4),
    ],
    ids=["counted, none failed", "failed, none counted", "more than results"],
)
def test_failed_must_count_the_failed_results(edit: Callable[[dict[str, Any]], Any]) -> None:
    """No picture asked, so the SVG rule cannot be what catches it."""
    with FakeRunner([_answer(edit)]) as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=runner.address)


def test_failed_counts_both_ways_when_right() -> None:
    def two_failed(request: dict[str, Any]) -> dict[str, Any]:
        return good_replay(request, failed=[(1, 1), (2, 2)])

    with FakeRunner([two_failed]) as runner:
        got = cad_engine.replay(_steps(), picture=False, address=runner.address)
    assert got.failed == 2 and [r.ok for r in got.results] == [False, True, False]


def _peak_mib(fn: Callable[[], Any]) -> float:
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        base = tracemalloc.get_traced_memory()[0]
        fn()
        return (tracemalloc.get_traced_memory()[1] - base) / (1024 * 1024)
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize("filler", [b"{},", b"[],", b"1,"])
def test_a_hostile_answer_of_many_containers_costs_little(filler: bytes) -> None:
    """8 MiB of `[{},{},...]` used to be parsed whole (about 220 MB); now it is refused first.
    The fake runner's own 8 MiB line counts in the peak too, so the bound is generous."""
    head, tail = b'{"v":1,"ok":true,"x":[', b"0]}\n"
    line = head + filler * ((cad_engine.ANSWER_MAX_BYTES - 64) // len(filler)) + tail
    assert len(line) <= cad_engine.ANSWER_MAX_BYTES

    def run() -> None:
        with FakeRunner([Raw(line)]) as runner, pytest.raises(EngineDown):
            cad_engine.replay(_steps(), picture=False, address=runner.address)

    peak = _peak_mib(run)
    print(f"peak {peak:.1f} MiB for {filler!r}")
    assert peak < 60


def test_brackets_and_commas_inside_strings_do_not_count() -> None:
    """A good answer for 2,000 commands whose texts are full of brackets and commas passes."""
    many = [step_from({"label": f"Lines {i}", "commands": [ROOM] * 400}) for i in range(5)]

    def answer(request: dict[str, Any]) -> dict[str, Any]:
        a = good_replay(request)
        for r in a["results"]:
            r["text"] = "[{,}]" * 60  # 300 characters
        a["texts"] = ['[,"{\\' * 16] * 80
        return a

    with FakeRunner([answer]) as runner:
        got = cad_engine.replay(many, picture=False, address=runner.address)
    assert len(got.results) == 2000


def test_too_many_containers_is_engine_down(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cad_engine.ANSWER_CONTAINERS_MAX == 5_000
    monkeypatch.setattr(cad_engine, "ANSWER_CONTAINERS_MAX", 3)
    with FakeRunner() as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=runner.address)


def test_too_many_commas_is_engine_down(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cad_engine.ANSWER_COMMAS_MAX == 20_000
    monkeypatch.setattr(cad_engine, "ANSWER_COMMAS_MAX", 10)
    with FakeRunner() as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=runner.address)


def test_an_answer_is_read_up_to_its_newline_without_waiting_for_the_close() -> None:
    line = json.dumps(failure("too_large")).encode() + b"\n"
    with FakeRunner([HoldOpen(line)]) as runner:
        started = time.monotonic()
        with pytest.raises(EngineRefused):
            cad_engine.replay(_steps(), picture=False, address=runner.address)
        assert time.monotonic() - started < 2


def test_bytes_after_the_newline_are_engine_down() -> None:
    line = json.dumps(failure("too_large")).encode() + b"\nx"
    with FakeRunner([HoldOpen(line)]) as runner, pytest.raises(EngineDown):
        cad_engine.replay(_steps(), picture=False, address=runner.address)


REAL_SHAPED = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8320.000000 7221.250000"'
    b' width="1600" height="1389">\n'
    b'<rect width="100%" height="100%" fill="white"/>\n'
    b'<polyline points="'
    + b" ".join(b"%d.000000,%d.500000" % (i, 7221 - i % 500) for i in range(4000))
    + b'" fill="none" stroke="black" stroke-width="12.500000" stroke-linecap="round"'
    b' stroke-linejoin="round"/>\n'
    b'<path d="M 100.000000 200.000000 L 300.000000 200.000000 L 300.000000 400.000000 Z"'
    b' fill="none" stroke="black" stroke-width="6.250000"/>\n'
    b"</svg>\n"
)


def test_check_svg_accepts_the_engines_real_attribute_forms() -> None:
    assert len(REAL_SHAPED) > 40_000
    check_svg(REAL_SHAPED)


REASONS = {"type", "size", "encoding", "markup", "xml", "element", "attribute", "value", "text",
           "depth", "count"}  # fmt: skip


@pytest.mark.parametrize("svg", REFUSED.values(), ids=REFUSED.keys())
def test_bad_picture_reasons_are_fixed_codes(svg: bytes) -> None:
    with pytest.raises(BadPicture) as caught:
        check_svg(svg)
    assert caught.value.reason in REASONS
    assert str(caught.value) == caught.value.reason


def test_a_bad_picture_reason_never_quotes_the_picture() -> None:
    for svg in (
        _svg("<Fictionalname/>"),
        _svg('<rect width="1" height="1" fictionalattr="1"/>'),
        _svg('<rect width="1" height="1" fill="url(fictional)"/>'),
    ):
        with pytest.raises(BadPicture) as caught:
            check_svg(svg)
        assert "ictional" not in str(caught.value) and "ictional" not in repr(caught.value)


NEAR_LIMIT = _svg("<g/>" * (400_000 - 1))


def test_check_svg_memory_stays_flat_near_the_element_limit() -> None:
    peak = _peak_mib(lambda: check_svg(NEAR_LIMIT))
    print(f"check_svg peak {peak:.1f} MiB on {len(NEAR_LIMIT):,} bytes")
    assert peak < 40


def test_check_svg_is_quick_near_the_element_limit() -> None:
    started = time.monotonic()
    check_svg(NEAR_LIMIT)
    took = time.monotonic() - started
    print(f"check_svg took {took:.2f} s")
    assert took < 2


def test_text_between_elements_is_still_found_when_children_are_freed() -> None:
    with pytest.raises(BadPicture, match=r"^text$"):
        check_svg(_svg("<g/>" * 100_000 + "x" + "<g/>" * 10))
    with pytest.raises(BadPicture, match=r"^text$"):
        check_svg(_svg("<g><g/><g/>x<g/></g>"))
    with pytest.raises(BadPicture, match=r"^text$"):
        check_svg(_svg("<g><g/><g/></g>x<g/>"))
    check_svg(_svg("<g>\n  <g/>\n  <g><path d='M 0 0'/></g>\n</g>\n"))

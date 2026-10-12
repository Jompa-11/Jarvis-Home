import pytest

from jarvis.core.tools import Risk, ToolRegistry, ToolResult, ToolSpec, ToolValidationError, schema


async def _noop(args, ctx):
    return ToolResult(ok=True)


def make_registry():
    r = ToolRegistry()
    r.register(
        ToolSpec(
            "set_level",
            "d",
            schema({"name": {"type": "string", "maxLength": 5}, "level": {"type": "integer", "minimum": 0, "maximum": 100}}, ["name", "level"]),
            _noop,
            Risk.LOW,
        )
    )
    return r


def test_valid_arguments_pass():
    spec, args = make_registry().validate("set_level", {"name": "a", "level": 10})
    assert spec.name == "set_level" and args["level"] == 10


@pytest.mark.parametrize(
    "name, args, fragment",
    [
        ("nope", {}, "Unknown tool"),
        ("set_level", "not-a-dict", "JSON object"),
        ("set_level", {"name": "a"}, "level"),
        ("set_level", {"name": "a", "level": 101}, "maximum"),
        ("set_level", {"name": "a", "level": "10"}, "integer"),
        ("set_level", {"name": "a", "level": 1, "shell": "rm -rf /"}, "Additional properties"),
        ("set_level", {"name": "toolongname", "level": 1}, "long"),
    ],
)
def test_invalid_arguments_rejected(name, args, fragment):
    with pytest.raises(ToolValidationError, match=fragment):
        make_registry().validate(name, args)


def test_bad_tool_definitions_rejected():
    with pytest.raises(ValueError):
        ToolSpec("Bad Name", "d", schema({}), _noop)
    with pytest.raises(ValueError):
        ToolSpec("ok_name", "d", {"type": "string"}, _noop)


def test_registry_is_sorted_and_unique():
    r = make_registry()
    r.register(ToolSpec("a_tool", "d", schema({}), _noop))
    assert [t.name for t in r.all()] == ["a_tool", "set_level"]
    with pytest.raises(ValueError):
        r.register(ToolSpec("a_tool", "d", schema({}), _noop))

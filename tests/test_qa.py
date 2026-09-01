from tools.qa import api_diff, schema_diff


def test_api_diff_detects_added_removed_changed():
    d = api_diff({"a": 1, "b": 2}, {"b": 3, "c": 4})
    assert d["added"] == {"c": 4}
    assert d["removed"] == {"a": 1}
    assert d["changed"] == {"b": [2, 3]}


def test_no_diff_is_empty():
    d = api_diff({"a": 1}, {"a": 1})
    assert not d["added"] and not d["removed"] and not d["changed"]


def test_schema_diff_reports_type_change():
    assert schema_diff({"id": "int"}, {"id": "str"})["changed"] == {"id": ["int", "str"]}

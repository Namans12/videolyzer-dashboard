"""Actual launcher serializer -> host parser -> launcher parser, without listeners."""
import importlib.util
import json
from pathlib import Path

import pytest

from app_host import parse_instance_record


def launcher_instance():
    # Source cross-repository contract only; this path is never part of the payload.
    spec = importlib.util.spec_from_file_location("plan4b_instance_contract",
        Path(r"D:\Video Comparizer\setup\app_instance.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_actual_instance_serializer_round_trips_through_host():
    instance = launcher_instance()
    actual = instance.make_record(install_id="i", build_id="b", session_id="a"*32,
        pid=123, process_created="133000000000000001", origin="http://127.0.0.1:49152",
        pipe_name=r"\\.\pipe\VideoSuite-test", control_secret="b"*64)
    parsed = parse_instance_record(json.loads(instance.encode_record(actual)), expected=actual)
    assert instance.parse_record(json.loads(json.dumps(parsed))) == actual


@pytest.mark.parametrize("key,value", [("install_id","other"), ("build_id","other"),
    ("session_id","c"*32), ("pid",456), ("process_created","2"),
    ("origin","http://127.0.0.1:49153"), ("control_secret","c"*64),
    ("origin","http://localhost:49152"), ("origin","http://192.168.1.1:49152")])
def test_mismatched_identity_or_non_ipv4_loopback_is_rejected(key, value):
    instance = launcher_instance()
    expected = instance.make_record(install_id="i", build_id="b", session_id="a"*32,
        pid=123, process_created="133000000000000001", origin="http://127.0.0.1:49152",
        pipe_name=r"\\.\pipe\VideoSuite-test", control_secret="b"*64)
    with pytest.raises(ValueError):
        parse_instance_record(dict(expected, **{key:value}), expected=expected)

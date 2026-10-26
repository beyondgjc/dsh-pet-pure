# -*- coding: utf-8 -*-
"""壳层配置加载（main.load_shell_config）：默认回退/下限钳制/锚点解析。"""

from __future__ import annotations

import json

from main import SHELL_CONFIG_DEFAULTS, load_shell_config


def test_missing_file_returns_defaults(tmp_path):
    assert load_shell_config(tmp_path / "nope.json") == SHELL_CONFIG_DEFAULTS


def test_broken_json_returns_defaults(tmp_path):
    path = tmp_path / "pet-pure.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_shell_config(path) == SHELL_CONFIG_DEFAULTS


def test_non_object_json_returns_defaults(tmp_path):
    path = tmp_path / "pet-pure.json"
    path.write_text("[1, 2]", encoding="utf-8")
    assert load_shell_config(path) == SHELL_CONFIG_DEFAULTS


def test_zone_size_clamped_to_minimum(tmp_path):
    """区比 sprite 帧还小宠会被裁——下限 500x330 是硬的。"""
    path = tmp_path / "pet-pure.json"
    path.write_text(json.dumps({"zone_width": 100, "zone_height": 50}), encoding="utf-8")
    config = load_shell_config(path)
    assert (config["zone_width"], config["zone_height"]) == (500, 330)


def test_zone_anchor_and_drag_flag(tmp_path):
    path = tmp_path / "pet-pure.json"
    path.write_text(json.dumps({"zone_x": 800, "zone_y": 40,
                                "drag_moves_zone": False}), encoding="utf-8")
    config = load_shell_config(path)
    assert (config["zone_x"], config["zone_y"]) == (800, 40)
    assert config["drag_moves_zone"] is False


def test_bad_anchor_falls_back_to_none(tmp_path):
    path = tmp_path / "pet-pure.json"
    path.write_text(json.dumps({"zone_x": "left"}), encoding="utf-8")
    config = load_shell_config(path)
    assert config["zone_x"] is None

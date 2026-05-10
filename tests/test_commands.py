"""User-defined slash commands: frontmatter parser, validation, dedup, size cap."""

from pathlib import Path

from src.infra.commands import load_commands


def _write(d: Path, name: str, text: str) -> Path:
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def test_loads_valid_command(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "recall.md",
        "---\nname: recall\ndescription: Search memory\n---\nBody here.",
    )
    cmds = load_commands(tmp_path)
    assert len(cmds) == 1
    assert cmds[0].name == "recall"
    assert cmds[0].description == "Search memory"
    assert cmds[0].body == "Body here."


def test_invalid_name_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "9bad.md",
        "---\nname: 9bad\ndescription: x\n---\nBody.",
    )
    assert load_commands(tmp_path) == []


def test_collision_with_builtin_skipped(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "start.md",
        "---\nname: start\ndescription: x\n---\nBody.",
    )
    assert load_commands(tmp_path) == []


def test_duplicate_name_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", "---\nname: foo\ndescription: a\n---\nBody A.")
    _write(tmp_path, "b.md", "---\nname: foo\ndescription: b\n---\nBody B.")
    cmds = load_commands(tmp_path)
    assert len(cmds) == 1


def test_empty_body_skipped(tmp_path: Path) -> None:
    _write(tmp_path, "x.md", "---\nname: x\ndescription: x\n---\n")
    assert load_commands(tmp_path) == []


def test_oversized_file_skipped(tmp_path: Path) -> None:
    p = _write(tmp_path, "big.md", "---\nname: big\ndescription: x\n---\nBody.")
    # 2 MB body exceeds the 1 MB cap in commands.py.
    p.write_bytes(b"x" * (2 * 1024 * 1024))
    assert load_commands(tmp_path) == []


def test_no_frontmatter_uses_filename(tmp_path: Path) -> None:
    _write(tmp_path, "plain.md", "Just a body.")
    cmds = load_commands(tmp_path)
    assert len(cmds) == 1
    assert cmds[0].name == "plain"
    assert cmds[0].description == "plain"


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert load_commands(tmp_path / "does-not-exist") == []

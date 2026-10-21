"""红线棘轮：超 600 行且没在文件头声明欠账的文件不许变多（AGENTS.md §9）。"""
from file_size_audit import UNDECLARED_RED_BASELINE, undeclared_red_files


def test_undeclared_red_files_not_above_baseline():
    files = undeclared_red_files()
    assert len(files) <= UNDECLARED_RED_BASELINE, (
        f"未声明欠账的红区文件 {len(files)} > 基线 {UNDECLARED_RED_BASELINE}："
        + ", ".join(f"{p}({n})" for p, n in files)
        + "；拆分或把基线调低，不要调高"
    )

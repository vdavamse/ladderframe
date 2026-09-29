from ladderframe import Runtime
from ladderframe.core.rootfs import Layer
from ladderframe.system.cron import build_crontab
from ladderframe.system.init import collect_scripts


def test_init_scripts_ordered_and_overridable(runtime: Runtime, tmp_path) -> None:
    share = Layer(tmp_path)
    share.init_d.mkdir(parents=True)
    (share.init_d / "01_first.sh").write_text("echo shared\n")
    (share.init_d / "00_base.sh").write_text("echo base\n")
    scripts = collect_scripts(runtime.root, share)
    assert [s.name for s in scripts] == ["00_base.sh", "01_first.sh", "02_second.sh"]
    assert scripts[1].parent == runtime.root.init_d  # root overrides share


def test_crontab(runtime: Runtime) -> None:
    crontab = build_crontab(runtime.root, None)
    assert "* * * * * echo hi" in crontab.read_text()

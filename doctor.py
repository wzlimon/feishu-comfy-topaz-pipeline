"""一键自检 / 排障工具.

用法:
    python doctor.py                      # 四段全检，告诉你哪里没配好
    python doctor.py --inspect-workflow   # 列出工作流里可注入提示词的节点（配置必用）
    python doctor.py --feishu-fields      # 列出飞书表格的真实字段名（对不上时用）
    python doctor.py --test-comfy "一只猫在跑"   # 只测 ComfyUI 出片
    python doctor.py --test-topaz D:/xx.mp4      # 只测 Topaz 超分
    python doctor.py --test-delivery D:/xx.mp4   # 只测网盘投递
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from core.config import PLACEHOLDER, load_config, setup_logging

OK = "[ OK ]"
NG = "[FAIL]"
WARN = "[WARN]"
SKIP = "[SKIP]"


def hr(title: str = "") -> None:
    if title:
        print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")
    else:
        print("-" * 60)


# ------------------------------------------------------------
# 各段检查
# ------------------------------------------------------------


def check_config(cfg) -> list[str]:
    hr("0. 配置文件")
    problems: list[str] = []
    must = [
        "feishu.app_id",
        "feishu.app_secret",
        "feishu.app_token",
        "feishu.table_id",
        "comfyui.inject.prompt_node",
    ]
    for key in must:
        val = cfg.get(key)
        if not val or (isinstance(val, str) and val.startswith(PLACEHOLDER)):
            print(f"{NG} {key:<32} 还是占位符，没填")
            problems.append(key)
        else:
            shown = str(val)
            if "secret" in key or key.endswith("app_id"):
                shown = shown[:8] + "…" + shown[-4:] if len(shown) > 14 else "已填"
            print(f"{OK} {key:<32} {shown}")
    return problems


def check_comfy(cfg) -> list[str]:
    hr("1. ComfyUI")
    from core.comfy import ComfyClient

    problems: list[str] = []
    client = ComfyClient(cfg)
    print(f"     地址 {client.base}")
    try:
        stats = client.ping()
        sysinfo = stats.get("system") or {}
        devs = stats.get("devices") or [{}]
        print(f"{OK} 服务在线   ComfyUI {sysinfo.get('comfyui_version', '?')}")
        for d in devs:
            total = int(d.get("vram_total") or 0) / 1024**3
            free = int(d.get("vram_free") or 0) / 1024**3
            print(f"     显卡 {d.get('name', '?')}  显存 {free:.1f}/{total:.1f} GB 空闲")
    except Exception as exc:
        print(f"{NG} 连不上：{exc}")
        print("     -> 确认 ComfyUI 已启动，端口和 config.yaml 里的 comfyui.port 一致")
        return ["comfyui 未启动"]

    # 工作流
    wf_path = cfg.path("comfyui.workflow")
    if not wf_path.exists():
        print(f"{NG} 工作流文件不存在: {wf_path}")
        print("     -> ComfyUI 界面里点 工作流 → 导出(API) ，存成这个路径")
        return problems + ["工作流缺失"]

    try:
        wf = client.load_workflow()
        print(f"{OK} 工作流     {wf_path.name}，{len(wf)} 个节点，API 格式正确")
    except Exception as exc:
        print(f"{NG} 工作流有问题：{exc}")
        return problems + ["工作流非法"]

    node = str(cfg.get("comfyui.inject.prompt_node", ""))
    field = str(cfg.get("comfyui.inject.prompt_field", "prompt"))
    if not node or node.startswith(PLACEHOLDER):
        print(f"{NG} 注入节点未配置")
        print("     -> 跑 python doctor.py --inspect-workflow 看该填哪个号")
        problems.append("inject.prompt_node 未配置")
    elif node not in wf:
        print(f"{NG} 工作流里没有节点 {node}")
        problems.append("inject.prompt_node 不存在")
    elif field not in (wf[node].get("inputs") or {}):
        print(f"{NG} 节点 {node} 里没有输入项 '{field}'")
        print(f"     该节点实际有：{list((wf[node].get('inputs') or {}).keys())}")
        problems.append("inject.prompt_field 不对")
    else:
        cur = str(wf[node]["inputs"][field])
        print(f"{OK} 注入点     节点 {node}（{wf[node].get('class_type')}）.{field}")
        print(f"     当前值   {cur[:70]}{'…' if len(cur) > 70 else ''}")
    return problems


def check_topaz(cfg) -> list[str]:
    hr("2. Topaz Video AI")
    if not cfg.get("topaz.enabled", True):
        print(f"{SKIP} 配置里已关闭超分")
        return []

    from core.topaz import TopazUpscaler

    up = TopazUpscaler(cfg)
    res = up.check()
    if res.get("ok"):
        print(f"{OK} ffmpeg     {cfg.get('topaz.ffmpeg')}")
        print(f"{OK} tvai_up    滤镜可用")
        print(f"{OK} 模型目录   {cfg.get('topaz.model_dir')}")
        model = cfg.get("topaz.model")
        mdir = Path(str(cfg.get("topaz.model_dir")))
        if (mdir / f"{model}.json").exists():
            print(f"{OK} 模型       {model}.json 存在")
        else:
            print(f"{WARN} 模型 {model}.json 没找到，可能会报 Model not found")
        print(
            f"     目标     {cfg.get('topaz.target_width')}x"
            f"{cfg.get('topaz.target_height')} / {cfg.get('topaz.encoder')} / "
            f"{cfg.get('topaz.bitrate')}"
        )
        return []

    for p in res.get("problems", []):
        print(f"{NG} {p}")
    return list(res.get("problems", []))


def check_delivery(cfg) -> list[str]:
    hr("3. 百度网盘投递")
    from core.delivery import NetdiskDelivery

    d = NetdiskDelivery(cfg)
    res = d.check()
    if res.get("ok"):
        print(f"{OK} 同步目录   {res.get('dir')}")
        print(f"     命名模板 {cfg.get('baidu.filename_template')}")
        sample = d.build_filename("一只橘猫在夕阳下奔跑，电影感", "recDemo123")
        print(f"     示例文件 {sample}")
        return []
    for p in res.get("problems", []):
        print(f"{NG} {p}")
    print("     -> 打开百度网盘客户端 → 设置 → 同步盘，确认本地文件夹路径")
    print("     -> 把它填进 config.yaml 的 baidu.sync_root")
    return list(res.get("problems", []))


def check_feishu(cfg) -> list[str]:
    hr("4. 飞书多维表格")
    from core.feishu import FeishuBitable

    fs = FeishuBitable(cfg)
    if not fs.configured:
        print(f"{NG} 凭证还是占位符，先按 README 第 2 步建应用拿 app_id / app_secret")
        return ["飞书凭证未填"]

    try:
        info = fs.ping()
        actual = fs.list_field_names()
    except Exception as exc:
        print(f"{NG} {exc}")
        return [str(exc)]

    print(f"{OK} 鉴权通过   表内共 {info.get('total')} 条记录")
    print(f"     实际字段 {'、'.join(actual) if actual else '（暂无任何列）'}")

    problems: list[str] = []
    configured = cfg.get("feishu.fields") or {}
    required = ("prompt", "status")
    print()
    for key, name in configured.items():
        if not name:
            continue
        if name in actual:
            print(f"{OK} {key:<12} -> 「{name}」")
        elif key in required:
            print(f"{NG} {key:<12} -> 「{name}」 表里没有这一列")
            problems.append(f"字段 {name} 不存在")
        else:
            print(f"{WARN} {key:<12} -> 「{name}」 表里没有，该项会被跳过")
    return problems


# ------------------------------------------------------------
# 辅助子命令
# ------------------------------------------------------------


def inspect_workflow(cfg) -> int:
    """列出工作流里所有可能塞提示词的节点."""
    from core.comfy import ComfyClient

    client = ComfyClient(cfg)
    wf_path = cfg.path("comfyui.workflow")
    if not wf_path.exists():
        print(f"{NG} 工作流文件不存在: {wf_path}")
        print()
        print("怎么导出：")
        print("  1. 打开 ComfyUI，加载你跑通的那个文生视频工作流")
        print("  2. 菜单 工作流 → 导出(API) / Save (API Format)")
        print(f"  3. 存成 {wf_path}")
        print("  注意：普通的「保存」格式不能用，必须是 API 格式")
        return 1

    try:
        found = client.inspect_workflow()
    except Exception as exc:
        print(f"{NG} {exc}")
        return 1

    hr("工作流里可注入文本的节点")
    if not found:
        print("没找到明显的文本输入节点。把工作流 JSON 发我，我帮你看。")
        return 1

    for item in found:
        print(f"\n节点 {item['node_id']}   class_type = {item['class_type']}")
        if item.get("title"):
            print(f"  标题: {item['title']}")
        for k, v in (item.get("text_inputs") or {}).items():
            preview = str(v).replace("\n", " ")[:100]
            print(f"  .{k} = {preview}{'…' if len(str(v)) > 100 else ''}")

    hr()
    print("怎么选：")
    print("  找到内容是你【正向提示词】的那个节点，把它的编号填进 config.yaml：")
    print("      comfyui:")
    print("        inject:")
    print(f"          prompt_node: \"{found[0]['node_id']}\"")
    key = next(iter((found[0].get("text_inputs") or {"prompt": ""}).keys()))
    print(f'          prompt_field: "{key}"')
    print("  如果有独立的负向提示词节点，同样填到 negative_node / negative_field")
    return 0


def feishu_fields(cfg) -> int:
    from core.feishu import FeishuBitable

    try:
        fs = FeishuBitable(cfg)
        names = fs.list_field_names()
    except Exception as exc:
        print(f"{NG} {exc}")
        return 1

    hr("飞书表格实际字段名")
    for n in names:
        print(f"  「{n}」")
    hr()
    print("把上面的名字**原样**抄进 config.yaml 的 feishu.fields，一个字都不能差。")
    return 0


def test_comfy(cfg, prompt: str) -> int:
    from core.comfy import ComfyClient

    hr(f"测试 ComfyUI 出片: {prompt}")
    client = ComfyClient(cfg)
    t0 = time.time()
    try:
        video = client.generate(prompt)
    except Exception as exc:
        print(f"{NG} {exc}")
        return 1
    print(f"{OK} 用时 {time.time() - t0:.0f} 秒")
    print(f"     产物 {video}")
    print(f"     大小 {video.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


def test_topaz(cfg, src: str) -> int:
    from core.topaz import TopazUpscaler

    srcp = Path(src)
    if not srcp.exists():
        print(f"{NG} 文件不存在: {srcp}")
        return 1

    hr(f"测试 Topaz 超分: {srcp.name}")
    up = TopazUpscaler(cfg)
    info = up.probe(srcp)
    if info:
        print(
            f"     源片 {info.get('width')}x{info.get('height')} "
            f"{float(info.get('duration') or 0):.1f}s"
        )
    dst = srcp.with_name(srcp.stem + "_1080p_test.mp4")
    t0 = time.time()
    try:
        out = up.upscale(srcp, dst)
    except Exception as exc:
        print(f"{NG} {exc}")
        return 1
    outinfo = up.probe(out)
    print(f"{OK} 用时 {time.time() - t0:.0f} 秒")
    print(f"     产物 {out}  {outinfo.get('width')}x{outinfo.get('height')}")
    print(f"     大小 {out.stat().st_size / 1024 / 1024:.1f} MB")
    return 0


def test_delivery(cfg, src: str) -> int:
    from core.delivery import NetdiskDelivery

    srcp = Path(src)
    if not srcp.exists():
        print(f"{NG} 文件不存在: {srcp}")
        return 1

    hr(f"测试网盘投递: {srcp.name}")
    d = NetdiskDelivery(cfg)
    name = d.build_filename("doctor 投递测试", "recDoctorTest")
    try:
        final = d.deliver(srcp, name)
    except Exception as exc:
        print(f"{NG} {exc}")
        return 1
    print(f"{OK} 已放入 {final}")
    print(f"     网盘路径 {d.netdisk_path(final)}")
    print(f"     等 {cfg.get('baidu.upload_wait_hint', 60)} 秒左右，手机上应该能看到")
    return 0


# ------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="全链路自检与排障",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--config", default=None)
    p.add_argument("--inspect-workflow", action="store_true", help="列出工作流可注入节点")
    p.add_argument("--feishu-fields", action="store_true", help="列出飞书表格字段名")
    p.add_argument("--test-comfy", metavar="PROMPT", help="测 ComfyUI 出片")
    p.add_argument("--test-topaz", metavar="VIDEO", help="测 Topaz 超分")
    p.add_argument("--test-delivery", metavar="VIDEO", help="测网盘投递")
    args = p.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        print(f"配置加载失败: {exc}", file=sys.stderr)
        return 2

    setup_logging(cfg)

    if args.inspect_workflow:
        return inspect_workflow(cfg)
    if args.feishu_fields:
        return feishu_fields(cfg)
    if args.test_comfy:
        return test_comfy(cfg, args.test_comfy)
    if args.test_topaz:
        return test_topaz(cfg, args.test_topaz)
    if args.test_delivery:
        return test_delivery(cfg, args.test_delivery)

    # 默认：全量体检
    print("\n飞书 -> ComfyUI -> Topaz -> 百度网盘   全链路体检")
    all_problems: list[str] = []
    all_problems += check_config(cfg)
    all_problems += check_comfy(cfg)
    all_problems += check_topaz(cfg)
    all_problems += check_delivery(cfg)
    all_problems += check_feishu(cfg)

    hr("体检结论")
    if not all_problems:
        print(f"{OK} 全部通过，直接跑 python main.py 就行")
        return 0

    print(f"发现 {len(all_problems)} 个问题：")
    for i, item in enumerate(all_problems, 1):
        print(f"  {i}. {item}")
    print("\n对照 README.md 里的「配置清单」逐项补齐，再跑一次本脚本。")
    return 1


if __name__ == "__main__":
    sys.exit(main())

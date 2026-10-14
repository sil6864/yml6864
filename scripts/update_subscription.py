#!/usr/bin/env python3
"""
从 link.txt 读取 Gist raw 链接，抓取 VLESS 节点，
合并去重后生成 FlClash 订阅文件。
"""

import os
import sys
import re
import yaml
import requests
from urllib.parse import urlparse
from pathlib import Path

# 配置
LINK_FILE = "link.txt"
OUTPUT_FILE = "subscription.yaml"
REQUEST_TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# 健康检查 URL（用于 url-test 延迟测试）
HEALTH_CHECK_URL = "http://www.gstatic.com/generate_204"


def read_links(filepath: str) -> list[str]:
    """读取 link.txt，返回所有非空、非注释的 URL。"""
    links = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # 只保留 http/https 链接
            if line.startswith(("http://", "https://")):
                links.append(line)
    return links


def fetch_content(url: str) -> str | None:
    """抓取单个 URL 的内容，失败返回 None。"""
    try:
        resp = requests.get(
            url,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        print(f"[WARN] 抓取失败: {url}\n  原因: {e}", file=sys.stderr)
        return None


def parse_proxies(text: str) -> list[dict]:
    """
    从文本中提取 proxies 列表。
    支持两种格式：
    1. 完整的 Clash YAML（包含 proxies:）
    2. 纯 JSON 行列表（每行一个 proxy 对象）
    """
    proxies = []

    # 尝试作为 YAML 解析
    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict) and "proxies" in data:
            if isinstance(data["proxies"], list):
                proxies.extend(data["proxies"])
                return proxies
    except yaml.YAMLError:
        pass

    # 尝试逐行解析 JSON 对象
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # 跳过 "proxies:" 这样的 YAML 键名行
        if line == "proxies:" or line.startswith("proxies:"):
            continue
        # 匹配以 { 开头的 JSON 对象
        if line.startswith("{"):
            try:
                obj = yaml.safe_load(line)
                if isinstance(obj, dict):
                    proxies.append(obj)
            except yaml.YAMLError:
                continue

    return proxies


def proxy_key(proxy: dict) -> tuple:
    """
    生成节点的唯一标识键，用于去重。
    基于 server + port + type 组合。
    """
    server = proxy.get("server", "")
    port = proxy.get("port", "")
    ptype = proxy.get("type", "")
    return (str(server), str(port), str(ptype))


def deduplicate(proxies: list[dict]) -> list[dict]:
    """按 (server, port, type) 去重，保留首次出现的节点。"""
    seen = set()
    result = []
    for p in proxies:
        key = proxy_key(p)
        if key not in seen:
            seen.add(key)
            result.append(p)
    return result


def build_subscription(proxies: list[dict]) -> dict:
    """
    构建完整的 FlClash 订阅配置。
    包含：
    - proxies: 节点列表
    - proxy-groups: 自动选择、故障转移、手动选择等策略组
    - rules: 基础分流规则
    """
    proxy_names = [p["name"] for p in proxies if "name" in p]

    if not proxy_names:
        raise ValueError("没有有效的节点可供生成订阅")

    config = {
        # 基础配置
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "external-controller": "127.0.0.1:9090",

        # DNS 配置（优化国内体验）
        "dns": {
            "enable": True,
            "listen": "0.0.0.0:1053",
            "ipv6": False,
            "enhanced-mode": "fake-ip",
            "fake-ip-range": "198.18.0.1/16",
            "nameserver": [
                "223.5.5.5",
                "119.29.29.29",
                "8.8.8.8",
                "1.1.1.1",
            ],
            "fallback": [
                "tls://8.8.8.8:853",
                "tls://1.1.1.1:853",
            ],
        },

        # 节点列表
        "proxies": proxies,

        # 策略组
        "proxy-groups": [
            # 自动选择：延迟最低
            {
                "name": "🚀 自动选择",
                "type": "url-test",
                "proxies": proxy_names,
                "url": HEALTH_CHECK_URL,
                "interval": 300,
                "tolerance": 50,
                "lazy": False,
            },
            # 故障转移：按顺序取第一个可用
            {
                "name": "🔄 故障转移",
                "type": "fallback",
                "proxies": proxy_names,
                "url": HEALTH_CHECK_URL,
                "interval": 300,
            },
            # 手动选择
            {
                "name": "🎯 手动选择",
                "type": "select",
                "proxies": ["🚀 自动选择", "🔄 故障转移"] + proxy_names,
            },
            # 全球直连
            {
                "name": "🌍 全球直连",
                "type": "select",
                "proxies": ["DIRECT", "🚀 自动选择", "🎯 手动选择"],
            },
            # 广告拦截
            {
                "name": "🛑 广告拦截",
                "type": "select",
                "proxies": ["REJECT", "DIRECT"],
            },
        ],

        # 分流规则
        "rules": [
            # 广告拦截
            "DOMAIN-SUFFIX,doubleclick.net,🛑 广告拦截",
            "DOMAIN-SUFFIX,googlesyndication.com,🛑 广告拦截",
            "DOMAIN-SUFFIX,googleadservices.com,🛑 广告拦截",

            # 国内直连
            "GEOIP,CN,🌍 全球直连",
            "DOMAIN-SUFFIX,cn,🌍 全球直连",
            "DOMAIN-KEYWORD,baidu,🌍 全球直连",
            "DOMAIN-KEYWORD,taobao,🌍 全球直连",
            "DOMAIN-KEYWORD,jd,🌍 全球直连",
            "DOMAIN-KEYWORD,bilibili,🌍 全球直连",

            # 默认走代理
            "MATCH,🎯 手动选择",
        ],
    }

    return config


def main():
    # 确定仓库根目录
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent

    link_path = repo_root / LINK_FILE
    output_path = repo_root / OUTPUT_FILE

    if not link_path.exists():
        print(f"[ERROR] 找不到 {link_path}", file=sys.stderr)
        sys.exit(1)

    # 1. 读取链接
    links = read_links(str(link_path))
    print(f"[INFO] 读取到 {len(links)} 个链接")

    if not links:
        print("[ERROR] link.txt 中没有有效链接", file=sys.stderr)
        sys.exit(1)

    # 2. 抓取所有内容
    all_proxies = []
    for i, url in enumerate(links, 1):
        print(f"[INFO] ({i}/{len(links)}) 抓取: {url}")
        text = fetch_content(url)
        if text is None:
            continue
        proxies = parse_proxies(text)
        print(f"       解析到 {len(proxies)} 个节点")
        all_proxies.extend(proxies)

    print(f"[INFO] 合计 {len(all_proxies)} 个节点（去重前）")

    # 3. 去重
    unique_proxies = deduplicate(all_proxies)
    print(f"[INFO] 去重后 {len(unique_proxies)} 个节点")

    if not unique_proxies:
        print("[ERROR] 没有有效节点", file=sys.stderr)
        sys.exit(1)

    # 4. 生成订阅配置
    subscription = build_subscription(unique_proxies)

    # 5. 写入文件
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(
            subscription,
            f,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
            width=4096,
        )

    print(f"[SUCCESS] 订阅已生成: {output_path}")
    print(f"          节点数: {len(unique_proxies)}")


if __name__ == "__main__":
    main()

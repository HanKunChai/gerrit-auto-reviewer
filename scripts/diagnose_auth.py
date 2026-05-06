"""Gerrit 认证诊断工具。
运行: python scripts/diagnose_auth.py
"""

import os
import sys
from pathlib import Path

# 确保在项目根目录
PROJECT_DIR = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_DIR)
sys.path.insert(0, str(PROJECT_DIR))

from mcp_gerrit_server.config import load_config, _load_dotenv
import requests

print("=" * 55)
print("  Gerrit 认证诊断")
print("=" * 55)

# 1. 检查 .env 文件
env_file = PROJECT_DIR / ".env"
print(f"\n[1] .env 文件: {env_file}")
if env_file.exists():
    content = env_file.read_text(encoding="utf-8")
    # 隐藏密码内容，只显示长度
    pw_line = [l for l in content.splitlines() if "PASSWORD" in l.upper()]
    for l in pw_line:
        key, _, val = l.partition("=")
        print(f"    {key.strip()}=*** ({len(val.strip())} 字符)")
else:
    print("    ❌ 未找到 .env 文件")
    print("    请在项目根目录创建 .env，内容: GERRIT_PASSWORD=你的HTTP密码")

# 2. 检查环境变量
print(f"\n[2] 环境变量 GERRIT_PASSWORD:")
pw_env = os.environ.get("GERRIT_PASSWORD", "")
if pw_env:
    print(f"    ✅ 已设置 ({len(pw_env)} 字符)")
    # 检查是不是登录密码（常见错误）
    if len(pw_env) < 10:
        print("    ⚠️  密码太短（< 10 字符），确认这是 Gerrit 生成的 HTTP Password 吗？")
else:
    print("    ❌ 未设置")

# 3. 加载配置
print(f"\n[3] 配置加载:")
try:
    cfg = load_config()
    gc = cfg.gerrit
    print(f"    mode:        {cfg.mode}")
    print(f"    gerrit URL:  {gc.base_url}")
    print(f"    username:    {gc.username}")
    print(f"    password:    {'已配置 (' + str(len(gc.password)) + ' 字符)' if gc.password else '❌ 为空'}")

    if not gc.password:
        print("\n    ❌ 密码为空，请检查 .env 或环境变量 GERRIT_PASSWORD")
        sys.exit(1)

    # 4. 测试 HTTP 连接
    print(f"\n[4] 测试 HTTP 连接:")

    # 测试基础连接（无认证）
    try:
        r = requests.get(gc.base_url, timeout=10)
        print(f"    GET {gc.base_url} → {r.status_code}")
        if r.status_code == 200:
            print("    ✅ Gerrit Web 可达")
        elif r.status_code == 302:
            print("    ✅ Gerrit Web 可达（重定向到登录页，正常）")
    except requests.exceptions.ConnectionError:
        print(f"    ❌ 无法连接: {gc.base_url}")
        print("    请检查地址是否正确，内网是否能 ping 通")
        sys.exit(1)
    except Exception as e:
        print(f"    ❌ 连接异常: {e}")
        sys.exit(1)

    # 测试认证 API（带 /a/ 前缀）
    print(f"\n[5] 测试认证 API:")
    for prefix in ["/a/", "/"]:
        auth_url = gc.base_url.rstrip("/") + prefix + "accounts/self"
        print(f"\n    GET {auth_url}")
        try:
            r = requests.get(
                auth_url,
                auth=(gc.username, gc.password),
                timeout=10,
            )
            print(f"    响应: {r.status_code}")

            if r.status_code == 200:
                body = r.text
                if body.startswith(")]}'\n"):
                    body = body[5:]
                import json
                data = json.loads(body)
                print(f"    ✅ 认证成功！前缀: {prefix}")
                print(f"    账号: {data.get('name', '未知')}")
                print(f"    账号ID: {data.get('_account_id', '未知')}")
                break
            elif r.status_code == 401:
                continue  # 试下一个前缀
            elif r.status_code == 404:
                print(f"    ℹ️  404 - 路径不对但认证通过（可能是 Nginx 代理路径问题）")
                break
            else:
                print(f"    ?  状态码: {r.status_code}")
                break
        except Exception as e:
            print(f"    ❌ 异常: {e}")
            break
    else:
        print(f"\n    ❌ 两种前缀都返回 401")
        print()
        print("    可能的原因：")
        print("    1. 密码不是 Gerrit 登录密码 → 去 Settings → HTTP Password 生成")
        print("    2. Nginx 反向代理拦截了 Authorization 头")
        print("       → 在 config.yaml 中添加: use_a_prefix: false")
        print("    3. 用户名错误 → 检查 config.yaml 中的 username")
        print("    4. Nginx 本身开了 auth_basic → 联系 Gerrit 管理员确认 Nginx 配置")

except Exception as e:
    print(f"    ❌ 配置加载失败: {e}")
    import traceback
    traceback.print_exc()

print()
print("=" * 55)

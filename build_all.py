#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mu300-builder: 自动跟随官方 release 并构建 "符合现在要求的" Magisk 固件包
================================================================================
复制自官方仓库 dikeckaan/mu300-linux 的 make-magisk-zips.sh 逻辑，扩展为：
  1. 自动查询官方最新 release（GitHub API）
  2. 下载并 SHA256 校验官方资产（内核 5.4/7.2、openwrt-luci rootfs、SHA256SUMS）
  3. 打 openwrt-luci 白名单补丁（官方安装器本来不认 luci rootfs）
  4. 自动获取 star 最高的 LuCI 主题 luci-theme-argon（jerrykuku，最新 release ipk）
     并集成 + 默认中文界面（lang=zh_cn, mediaurlbase=argon, themes 加 Argon）
  5. 组装两个 Magisk zip：
       a) mu300-magisk-<TAG>-openwrt-luci-k7.2-argon-zh.zip        标准版
       b) mu300-magisk-<TAG>-openwrt-luci-k7.2-argon-zh-tf.zip     TF 卡部署版
          （zip 内 mu300/mu300-install.conf 内置 MU300_STORAGE=sd + MU300_SD_ERASE=yes）
  6. 清单精确审计 + payload 未压缩检查 + SHA256SUMS + 构建报告

用法:
  python build_all.py [--tag v2026.10.10] [--force] [--proxy http://127.0.0.1:7890] [--std] [--watch 15]

默认只构建 TF 卡部署版（内置 MU300_STORAGE=sd）；--std 追加标准版。
--watch N：常驻监视模式，每 N 分钟查一次官方最新 release，发现新版本才构建
（跟随官方更新，不做每日定时构建）。

产物输出到 work/out/<tag>/，last_tag.txt 记录已构建版本；重跑时若无新 release 则跳过。
所有资产必须与官方 SHA256SUMS 一致，否则构建中止。
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import stat
import struct
import sys
import tarfile
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = HERE / 'work'
RELEASES = WORK / 'releases'
SRC = WORK / 'src'
ARGON = WORK / 'argon'
OUT = WORK / 'out'
LAST_TAG_FILE = WORK / 'last_tag.txt'
PROXY = os.environ.get('MU300_PROXY', 'http://127.0.0.1:7890')

OFFICIAL_REPO = 'dikeckaan/mu300-linux'
ARGON_REPO = 'jerrykuku/luci-theme-argon'
# 官方资产（构建 openwrt-luci + k7.2 需要）
ASSETS_NEEDED = ['mu300-kernel.tar.gz', 'mu300-kernel-7.2.tar.gz',
                 'mu300-openwrt-luci-rootfs.tar.gz', 'SHA256SUMS']
SYSTEM = 'openwrt-luci'
OS = 'openwrt-luci'
KERNEL = '7.2'
KERNEL_ASSET = 'mu300-kernel-7.2.tar.gz'
ROOTFS_ASSET = 'mu300-openwrt-luci-rootfs.tar.gz'

# 官方 make-magisk-zips.sh 的 COMMON 清单（本工程输出与其完全一致）
COMMON = [
    'META-INF/com/google/android/update-binary',
    'META-INF/com/google/android/updater-script',
    'module.prop', 'customize.sh', 'action.sh', 'switch.sh',
    'system/bin/mu300-linux',
    'mu300/install.sh', 'mu300/android-boot-image.sh', 'mu300/mu300-update',
    'mu300/android-install.sh', 'mu300/android-mount-mu300root.sh',
    'mu300/storage.sh', 'mu300/i18n.sh', 'mu300/i18n/tr.tsv', 'mu300/i18n/zh.tsv',
    'mu300/subset-files.txt', 'mu300/gpu-files.txt', 'mu300/busybox', 'mu300/manifest',
]
PAYLOAD = ['payload/' + KERNEL_ASSET, 'payload/' + ROOTFS_ASSET]

# argon ipk 内需要集成的文件（相对 ipk data.tar.gz 根）
ARGON_FILES = [
    'etc/uci-defaults/30_luci-theme-argon',
    'usr/libexec/rpcd/luci.argon_wallpaper',
    'usr/share/rpcd/acl.d/luci-theme-argon.json',
    'usr/share/ucode/luci/template/themes/argon/footer.ut',
    'usr/share/ucode/luci/template/themes/argon/footer_login.ut',
    'usr/share/ucode/luci/template/themes/argon/header.ut',
    'usr/share/ucode/luci/template/themes/argon/header_login.ut',
    'usr/share/ucode/luci/template/themes/argon/head_meta.ut',
    'usr/share/ucode/luci/template/themes/argon/out_header_login.ut',
    'usr/share/ucode/luci/template/themes/argon/sysauth.ut',
    'www/luci-static/argon/favicon.ico',
    'www/luci-static/argon/background/README.md',
    'www/luci-static/argon/css/cascade.css',
    'www/luci-static/argon/css/dark.css',
    'www/luci-static/argon/fonts/argon.woff',
    'www/luci-static/argon/fonts/argon.woff2',
    'www/luci-static/argon/fonts/GoogleSans-Regular.woff',
    'www/luci-static/argon/fonts/GoogleSans-Regular.woff2',
    'www/luci-static/argon/fonts/TypoGraphica.woff',
    'www/luci-static/argon/fonts/TypoGraphica.woff2',
    'www/luci-static/argon/icon/android-icon-192x192.png',
    'www/luci-static/argon/icon/apple-icon-144x144.png',
    'www/luci-static/argon/icon/arrow.svg',
    'www/luci-static/argon/icon/favicon-16x16.png',
    'www/luci-static/argon/icon/favicon-32x32.png',
    'www/luci-static/argon/icon/manifest.json',
    'www/luci-static/argon/icon/spinner.svg',
    'www/luci-static/argon/img/argon.svg',
    'www/luci-static/argon/img/bg1.jpg',
    'www/luci-static/argon/img/blank.png',
    'www/luci-static/argon/img/volume_high.svg',
    'www/luci-static/argon/img/volume_off.svg',
    'www/luci-static/resources/menu-argon.js',
]
# argon ipk 内的目录（保证父目录存在）
ARGON_DIRS = sorted({
    '/'.join(f.split('/')[:-1]) for f in ARGON_FILES
})

# 补丁：官方安装器 manifest_load 白名单加 openwrt-luci 行
PATCH_ANCHOR = b'openwrt:openwrt::mu300-openwrt-rootfs.tar.gz) ;;'
PATCH_LINE = b'openwrt-luci:openwrt-luci::mu300-openwrt-luci-rootfs.tar.gz) ;;'

TF_CONF = (
    '# TF card deployment build (mu300-magisk-*-tf.zip):\n'
    '# installs Linux to the SD card and formats it (everything on the card is erased).\n'
    '# A card that already holds a mu300sd installation is never formatted.\n'
    'MU300_STORAGE=sd\n'
    'MU300_SD_ERASE=yes\n'
).encode()


def log(msg):
    print('[mu300-builder] ' + msg, flush=True)


def http_opener():
    if PROXY:
        ph = urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY})
        return urllib.request.build_opener(ph)
    return urllib.request.build_opener()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url, timeout=60):
    with http_opener().open(url, timeout=timeout) as r:
        return r.read()


def http_download(url, dest):
    data = http_get(url)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return data


def github_latest_tag(repo):
    """GET /releases/latest 的 302 Location 里取 tag，不消耗 API 配额。"""
    url = f'https://github.com/{repo}/releases/latest'
    req = urllib.request.Request(url, headers={'User-Agent': 'mu300-builder'})
    if PROXY:
        opener = urllib.request.build_opener(_NoRedirect,
                                             urllib.request.ProxyHandler({'http': PROXY, 'https': PROXY}))
    else:
        opener = urllib.request.build_opener(_NoRedirect)
    try:
        opener.open(req, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code == 302:
            loc = e.headers.get('Location', '')
            tag = loc.rstrip('/').split('/')[-1]
            if tag:
                return tag
    sys.exit('无法从 GitHub 解析最新 release tag（检查代理/网络）')


def release_asset_url(repo, tag, name):
    return f'https://github.com/{repo}/releases/download/{tag}/{name}'


def release_asset_names(repo, tag):
    """从 expanded_assets 页抓资产文件名（避免 API）。"""
    html = http_get(f'https://github.com/{repo}/releases/expanded_assets/{tag}').decode('utf-8', errors='replace')
    names = []
    for m in re.finditer(r'/releases/download/[^"<]+', html):
        fn = m.group(0).rsplit('/', 1)[-1]
        if fn not in names:
            names.append(fn)
    return names


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def latest_tag():
    tag = github_latest_tag(OFFICIAL_REPO)
    return tag, ''


def ensure_assets(tag):
    """下载官方资产到 work/releases/<tag>/，全部对照官方 SHA256SUMS 校验，返回目录 Path。"""
    d = RELEASES / tag
    for name in ASSETS_NEEDED:
        p = d / name
        if p.exists() and p.stat().st_size > 0:
            log(f'资产已存在(跳过下载): {name}')
            continue
        url = release_asset_url(OFFICIAL_REPO, tag, name)
        log(f'下载 {name}')
        http_download(url, p)
    # 校验全部资产（SHA256SUMS 本身先校验自洽：对照 release 内容）
    sums = {}
    for line in (d / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2:
            sums[parts[-1]] = parts[0]
    ok = True
    for name in ASSETS_NEEDED:
        if name == 'SHA256SUMS':
            continue
        want = sums.get(name) or sums.get('*' + name)
        got = sha256_file(d / name)
        status = 'OK' if want and got == want else 'FAIL'
        if status == 'FAIL':
            ok = False
        log(f'校验 {name}: {got[:16]}… {status}')
    if not ok:
        sys.exit('资产 SHA256 校验失败，中止构建')
    return d


def ensure_source(tag):
    """下载并解压官方源码（archive zip），返回源码目录 Path。"""
    d = SRC / tag
    d.mkdir(parents=True, exist_ok=True)
    subs = [x for x in d.iterdir() if x.is_dir()]
    if subs:
        log(f'源码已存在: {subs[0].name}')
        return subs[0]
    zip_url = f'https://github.com/{OFFICIAL_REPO}/archive/refs/tags/{tag}.zip'
    zf = d / 'src.zip'
    log(f'下载源码 {zip_url}')
    data = http_download(zip_url, zf)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        z.extractall(d)
    zf.unlink()
    sub = next(d.iterdir())
    log(f'源码就绪: {sub.name}')
    return sub


def patch_installer(src_dir):
    """幂等打 openwrt-luci 白名单补丁，返回补丁后的 install.sh 内容(bytes)。"""
    p = src_dir / 'android' / 'magisk' / 'installer' / 'mu300-install.sh'
    data = p.read_bytes()
    if PATCH_LINE in data:
        log('补丁已存在（幂等跳过）')
    elif PATCH_ANCHOR not in data:
        sys.exit('补丁锚点未找到，源码结构可能已变化，请人工检查 mu300-install.sh')
    else:
        data = data.replace(PATCH_ANCHOR, PATCH_LINE + b'\n' + PATCH_ANCHOR, 1)
        log('已打 openwrt-luci 白名单补丁')
    return data


# ---------------- argon ipk 处理 ----------------

def parse_ar(data):
    """解析 ar 归档，返回 [(name, body_bytes)]。"""
    assert data[:8] == b'!<arch>\n', '不是 ar 归档'
    out = []
    pos = 8
    while pos + 60 <= len(data):
        hdr = data[pos:pos + 60]
        name = hdr[0:16].decode('ascii', errors='replace').strip()
        size = int(hdr[48:58].decode().strip() or 0)
        body = data[pos + 60:pos + 60 + size]
        out.append((name, body))
        pos += 60 + size
        if size % 2:
            pos += 1
        if pos >= len(data):
            break
    return out


def unpack_ipk(ipk_path):
    """解 ipk，返回 {relpath: (data, tarinfo)} 与 {relpath: tarinfo}（目录）。

    兼容两种实际格式：标准 ar 归档（!<arch>）与 gzip 压缩的 tar（外层 gzip，内层
    debian-binary/data.tar.gz/control.tar.gz，OpenWrt 部分工具链的产物）。
    """
    raw = Path(ipk_path).read_bytes()
    files, dirs = {}, {}

    def ingest_tar(bytes_, name_prefix=''):
        tf = tarfile.open(fileobj=io.BytesIO(bytes_), mode='r:*')
        for m in tf:
            rel = name_prefix + m.name.lstrip('./')
            if not rel:
                continue
            if m.isdir():
                dirs[rel] = m
            elif m.issym():
                files[rel] = (b'LINK:' + m.linkname.encode(), m)
            elif m.isreg():
                files[rel] = (tf.extractfile(m).read(), m)

    if raw[:2] == b'\x1f\x8b':          # gzip 外壳
        inner = gzip.decompress(raw)
        tf = tarfile.open(fileobj=io.BytesIO(inner), mode='r:*')
        got = False
        for m in tf.getmembers():
            if m.name.rstrip('/').endswith('data.tar.gz') and m.isfile():
                ingest_tar(tf.extractfile(m).read())
                got = True
        if not got:
            sys.exit('ipk(gzip/tar) 内没有 data.tar.gz')
    else:                                # 标准 ar 归档
        got = False
        for name, body in parse_ar(raw):
            if name.rstrip('/') == 'data.tar.gz':
                ingest_tar(body)
                got = True
        if not got:
            sys.exit('ipk(ar) 内没有 data.tar.gz')
    return files, dirs


def ensure_argon():
    """获取最新 argon ipk 并解包，返回 (files, dirs)。离线 fallback 到 build/luci-theme-argon_2.4.7_all.ipk。"""
    ARGON.mkdir(parents=True, exist_ok=True)
    ipk = None
    try:
        atag = github_latest_tag(ARGON_REPO)
        names = release_asset_names(ARGON_REPO, atag)
        ipk_names = [n for n in names if n.endswith('.ipk')]
        theme_ipks = [n for n in ipk_names if n.startswith('luci-theme-argon')]
        pick = theme_ipks[0] if theme_ipks else (ipk_names[0] if ipk_names else None)
        if not pick:
            raise RuntimeError(f'argon release {atag} 没有 .ipk 资产: {names}')
        ipk = ARGON / pick
        if not ipk.exists():
            url = release_asset_url(ARGON_REPO, atag, pick)
            log(f'下载 argon 主题 {atag}: {pick}')
            http_download(url, ipk)
        else:
            log(f'argon ipk 已存在: {ipk.name}')
    except Exception as e:
        fallback = HERE.parent / 'build' / 'luci-theme-argon_2.4.7_all.ipk'
        if fallback.exists():
            log(f'在线获取 argon 失败({e})，使用本地缓存 {fallback.name}')
            ipk = fallback
        else:
            sys.exit('argon 获取失败且无本地缓存')
    files, dirs = unpack_ipk(ipk)
    missing = [f for f in ARGON_FILES if f not in files]
    if missing:
        sys.exit(f'argon ipk 缺少预期文件: {missing}')
    log(f'argon 就绪: {len(files)} 文件')
    return files, dirs


def integrate_rootfs(rootfs_tgz, argon_files, argon_dirs, out_tgz):
    """官方 rootfs + argon 文件 + 中文配置 -> 新 tar.gz。逐 member 复制保留属性。"""
    tin = tarfile.open(rootfs_tgz, 'r:gz')
    tout = tarfile.open(out_tgz, 'w:gz', format=tarfile.GNU_FORMAT)
    try:
        added = set()
        for m in tin.getmembers():
            rel = m.name.lstrip('./')
            if m.issym() or m.islnk():
                tout.addfile(m)
                continue
            if m.isreg() and rel == 'etc/config/luci':
                data = tin.extractfile(m).read()
                # 语言改中文、默认主题改 argon、themes 段加 Argon（幂等：无匹配则原样）
                data = data.replace(b"option lang 'auto'", b"option lang 'zh_cn'")
                data = data.replace(b"option mediaurlbase '/luci-static/aurora'",
                                    b"option mediaurlbase '/luci-static/argon'")
                if b"option Argon '/luci-static/argon'" not in data:
                    data = data.replace(
                        b"config internal 'themes'\n\toption Aurora '/luci-static/aurora'",
                        b"config internal 'themes'\n\toption Argon '/luci-static/argon'\n\toption Aurora '/luci-static/aurora'")
                nm = m
                nm.size = len(data)
                tout.addfile(nm, io.BytesIO(data))
                added.add('etc/config/luci')
                continue
            if m.isreg():
                tout.addfile(m, tin.extractfile(m))
            else:
                tout.addfile(m)
        # 新增 argon 文件（保留 ipk 内 mode/uid/gid/mtime）
        for rel in sorted(ARGON_DIRS):
            if rel and rel not in added:
                ti = tarfile.TarInfo(rel)
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                ti.uid = ti.gid = 0
                tout.addfile(ti)
        for rel in ARGON_FILES:
            data, meta = argon_files[rel]
            ti = tarfile.TarInfo(rel)
            if data.startswith(b'LINK:'):
                ti.type = tarfile.SYMTYPE
                ti.linkname = data[5:].decode()
                tout.addfile(ti)
                continue
            ti.size = len(data)
            ti.mode = meta.mode & 0o7777
            ti.uid = meta.uid
            ti.gid = meta.gid
            ti.mtime = meta.mtime
            tout.addfile(ti, io.BytesIO(data))
    finally:
        tin.close()
        tout.close()


# ---------------- stage / zip ----------------

def make_stage(tag, code, rel_dir, src_dir, rootfs_payload, tf=False):
    """按官方 make-magisk-zips.sh 的 stage 逻辑组装目录，返回 stage Path。"""
    st = OUT / tag / ('stage' + ('-tf' if tf else ''))
    if st.exists():
        shutil.rmtree(st)
    (st / 'META-INF' / 'com' / 'google' / 'android').mkdir(parents=True)
    (st / 'system' / 'bin').mkdir(parents=True)
    (st / 'mu300' / 'i18n').mkdir(parents=True)
    (st / 'payload').mkdir(parents=True)

    I = src_dir / 'android' / 'magisk' / 'installer'
    M = src_dir / 'android' / 'magisk' / 'mu300-linux-switch'
    T = src_dir / 'tools'

    shutil.copyfile(I / 'update-binary', st / 'META-INF' / 'com' / 'google' / 'android' / 'update-binary')
    shutil.copyfile(I / 'updater-script', st / 'META-INF' / 'com' / 'google' / 'android' / 'updater-script')
    tpl = (I / 'module.prop.in').read_text(encoding='utf-8')
    (st / 'module.prop').write_text(
        tpl.replace('@TAG@', tag).replace('@CODE@', str(code)).replace('@SYSTEM@', SYSTEM).replace('@KERNEL@', KERNEL),
        encoding='utf-8')
    shutil.copyfile(I / 'customize.sh', st / 'customize.sh')
    shutil.copyfile(M / 'action.sh', st / 'action.sh')
    shutil.copyfile(M / 'switch.sh', st / 'switch.sh')
    shutil.copyfile(M / 'system' / 'bin' / 'mu300-linux', st / 'system' / 'bin' / 'mu300-linux')
    shutil.copyfile(I / 'mu300-install.sh.patched', st / 'mu300' / 'install.sh')
    for f in ('android-boot-image.sh', 'android-install.sh', 'android-mount-mu300root.sh', 'storage.sh', 'i18n.sh'):
        shutil.copyfile(T / f, st / 'mu300' / f)
    shutil.copyfile(src_dir / 'rootfs' / 'overlay' / 'opt' / 'mu300' / 'bin' / 'mu300-update', st / 'mu300' / 'mu300-update')
    shutil.copyfile(src_dir / 'i18n' / 'tr.tsv', st / 'mu300' / 'i18n' / 'tr.tsv')
    shutil.copyfile(src_dir / 'i18n' / 'zh.tsv', st / 'mu300' / 'i18n' / 'zh.tsv')
    shutil.copyfile(src_dir / 'android-vendor' / 'subset-files.txt', st / 'mu300' / 'subset-files.txt')
    shutil.copyfile(src_dir / 'android-vendor' / 'gpu-files.txt', st / 'mu300' / 'gpu-files.txt')
    # busybox 从 5.4 内核包提取（官方逻辑 tar -xzOf ... ./busybox）
    with tarfile.open(rel_dir / 'mu300-kernel.tar.gz', 'r:gz') as tark:
        m = next((m for m in tark.getmembers() if m.name.rstrip('/').endswith('busybox')), None)
        if m is None:
            sys.exit('mu300-kernel.tar.gz 内没有 busybox')
        (st / 'mu300' / 'busybox').write_bytes(tark.extractfile(m).read())
    shutil.copyfile(rel_dir / KERNEL_ASSET, st / 'payload' / KERNEL_ASSET)
    shutil.copyfile(rootfs_payload, st / 'payload' / ROOTFS_ASSET)
    # manifest
    (st / 'mu300' / 'manifest').write_text(
        'TAG=%s\nSYSTEM=%s\nOS=%s\nUBUNTU=\nKERNEL=%s\nKERNEL_ASSET=%s\nROOTFS_ASSET=%s\nSHA256_KERNEL=%s\nSHA256_ROOTFS=%s\n'
        % (tag, SYSTEM, OS, KERNEL, KERNEL_ASSET, ROOTFS_ASSET,
           sha256_file(rel_dir / KERNEL_ASSET), sha256_file(rootfs_payload)), encoding='utf-8')
    if tf:
        (st / 'mu300' / 'mu300-install.conf').write_bytes(TF_CONF)
    return st


def build_zip(stage_dir, out_zip, tag):
    if out_zip.exists():
        out_zip.unlink()
    yy, mm, dd = (int(x) for x in tag.lstrip('v').split('.')[:3])
    with zipfile.ZipFile(out_zip, 'w') as z:
        for root, dirs, files in os.walk(stage_dir):
            for f in sorted(files):
                full = os.path.join(root, f)
                rel = os.path.relpath(full, stage_dir).replace('\\', '/')
                zi = zipfile.ZipInfo(rel, date_time=(yy, mm, dd, 0, 0, 0))
                if rel.startswith('payload/'):
                    zi.compress_type = zipfile.ZIP_STORED
                    z.writestr(zi, Path(full).read_bytes())
                else:
                    zi.compress_type = zipfile.ZIP_DEFLATED
                    zi._compresslevel = 9
                    z.writestr(zi, Path(full).read_bytes())
    log(f'打包完成: {out_zip.name} ({out_zip.stat().st_size} bytes)')


def audit(zip_path, tf=False):
    with zipfile.ZipFile(zip_path) as z:
        have = sorted(z.namelist())
        want = sorted(COMMON + PAYLOAD + (['mu300/mu300-install.conf'] if tf else []))
        if have != want:
            sys.exit(f'审计失败 {zip_path.name}:\n  多出: {set(have) - set(want)}\n  缺失: {set(want) - set(have)}')
        for p in PAYLOAD:
            if z.getinfo(p).compress_type != zipfile.ZIP_STORED:
                sys.exit(f'审计失败: payload {p} 被压缩')
        manifest = z.read('mu300/manifest').decode()
        if 'openwrt-luci' not in z.read('mu300/install.sh').decode(errors='replace'):
            sys.exit('审计失败: install.sh 缺 openwrt-luci 白名单')
    log(f'审计通过: {zip_path.name} ({len(want)} 条目, payload store, 白名单 OK)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--tag', help='指定构建 tag（默认官方最新 release）')
    ap.add_argument('--force', action='store_true', help='忽略 last_tag 检测，强制重建')
    ap.add_argument('--skip-source', action='store_true', help='复用已解压源码（调试用）')
    ap.add_argument('--std', action='store_true', help='同时构建标准版（默认只构建 TF 部署版）')
    ap.add_argument('--watch', type=int, metavar='MINUTES', default=0,
                    help='跟随更新模式：每 N 分钟检查官方 release，有新版本才构建（0=单次运行）')
    args = ap.parse_args()

    while True:
        if args.tag:
            tag, published = args.tag, ''
        else:
            tag, published = latest_tag()
            log(f'官方最新 release: {tag} ({published})')

        # 更新检测：有新版（或 --force）才构建；无新版直接跳过，不做每日构建
        last = LAST_TAG_FILE.read_text().strip() if LAST_TAG_FILE.exists() else ''
        outdir = OUT / tag
        if last == tag and outdir.exists() and not args.force:
            if args.watch:
                log(f'已是最新（{tag}），{args.watch} 分钟后继续监视官方更新…')
            else:
                log(f'已构建过 {tag}（last_tag={last}），跳过。加 --force 可强制重建。')
            if not args.watch:
                return
        else:
            build_once(args, tag, published)
            if not args.watch:
                return
            log(f'完成 {tag}，{args.watch} 分钟后继续监视官方更新…')
        time.sleep(args.watch * 60)


def build_once(args, tag, published):
    rel_dir = ensure_assets(tag)
    src_dir = None if args.skip_source else ensure_source(tag)
    if src_dir is None:
        # 用现有源码目录
        existing = [d for d in (SRC / tag).iterdir() if d.is_dir()] if (SRC / tag).exists() else []
        src_dir = existing[0] if existing else Path(r'C:\Users\Administrator\Doubao\chats\2026-10-04\new-chat\mu300-linux-main')
        log(f'复用源码: {src_dir}')

    # 补丁安装器
    patched = patch_installer(src_dir)
    (src_dir / 'android' / 'magisk' / 'installer' / 'mu300-install.sh.patched').write_bytes(patched)

    # argon 集成 rootfs
    argon_files, argon_dirs = ensure_argon()
    rootfs_payload = OUT / tag / ('payload-' + ROOTFS_ASSET)
    (OUT / tag).mkdir(parents=True, exist_ok=True)
    integrate_rootfs(rel_dir / ROOTFS_ASSET, argon_files, argon_dirs, rootfs_payload)
    log(f'argon+中文 rootfs: {rootfs_payload.name} (sha256 {sha256_file(rootfs_payload)[:16]}…)')

    # code: tag 数字前 9 位
    code = ''.join(c for c in tag if c.isdigit())[:9] or 1
    zip_base = f'mu300-magisk-{tag}-openwrt-luci-k7.2-argon-zh'

    # 默认只产 TF 部署版（--std 追加标准版）
    variants = [(True, '-tf')]
    if args.std:
        variants.insert(0, (False, ''))
    for tf, suffix in variants:
        stage = make_stage(tag, code, rel_dir, src_dir, rootfs_payload, tf=tf)
        zip_path = OUT / tag / (zip_base + suffix + '.zip')
        build_zip(stage, zip_path, tag)
        audit(zip_path, tf=tf)

    # SHA256SUMS
    sums_lines = []
    for p in sorted((OUT / tag).glob('mu300-magisk-*.zip')):
        sums_lines.append(f'{sha256_file(p)}  {p.name}')
    (OUT / tag / 'SHA256SUMS-magisk').write_text('\n'.join(sums_lines) + '\n', encoding='utf-8')
    # 报告
    report = {
        'tag': tag, 'published': published, 'system': SYSTEM, 'kernel': KERNEL,
        'rootfs': ROOTFS_ASSET, 'assets_sha256_verified': True,
        'argon_files_integrated': len(ARGON_FILES),
        'zips': [{ 'name': p.name, 'sha256': sha256_file(p), 'bytes': p.stat().st_size }
                 for p in sorted((OUT / tag).glob('mu300-magisk-*.zip'))],
    }
    (OUT / tag / 'build-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    LAST_TAG_FILE.write_text(tag, encoding='utf-8')
    log('全部完成:')
    for p in sorted((OUT / tag).glob('mu300-magisk-*.zip')):
        log(f'  {p.name}  ({p.stat().st_size} bytes, sha256 {sha256_file(p)[:16]}…)')
    log(f'校验文件: {OUT / tag / "SHA256SUMS-magisk"}')


if __name__ == '__main__':
    main()

# Third-Party Notices — dsh-pet-pure

本仓库分发的第三方软件与素材，按其各自许可声明如下。代码许可（MIT，
双上游）见仓库根目录 `LICENSE`；**角色动画素材不适用 MIT**，见第 1 节。

## 1. Character Animation Assets（深深 / OC「溟月」webm 素材）

- **Files**: `assets/characters/shenshen/videos/**`
  （640×360 / 24fps / VP9-alpha 透明 webm）
- **Origin / Source Repository**:
  [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet)
  （动作素材做法与素材来源），经
  [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)
  整理搬运，本仓库随源码继续分发。
- **作者**: 角色 OC「溟月」出自画师 **上善无形**，素材由社区成员整理制作。
- **License**: CC BY-NC-SA 类条款——**仅限个人非商业使用**；使用须保留
  署名与来源；不得用于任何商业 / 盈利场景。派生物（含本仓库代码与
  打包产物）继续遵循同样约束。

## 2. Upstream Code（MIT）

- [PC2005-cloud/dsh-pet](https://github.com/PC2005-cloud/dsh-pet) — MIT
  License，Copyright (c) 2026 PC2005-cloud（动画素材最初来源仓库）
- [MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop) —
  MIT License，Copyright (c) 2026 Merzlin（渲染闭包剥离来源，`pet/`
  27 模块与其 LICENSE 正文随本仓库 `LICENSE` 分发）

## 3. 随包依赖（pip 安装，许可各归其包）

- PySide6-Essentials / shiboken6 — LGPL-3.0 / GPL 组合条款（Qt 动态链接）
- imageio-ffmpeg — BSD-2-Clause（内含 ffmpeg 二进制，另见其构建仓库许可）

> 上游完整第三方声明（本项目未搬运的组件：lunar-python、PresentMon、
> OCR、节日文案等）见
> [MerZlin/dsh-pet-indesktop 的 THIRD_PARTY_NOTICES.md](https://github.com/MerZlin/dsh-pet-indesktop/blob/main/THIRD_PARTY_NOTICES.md)。

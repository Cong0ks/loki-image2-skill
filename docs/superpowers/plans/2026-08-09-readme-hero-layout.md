# README Hero Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the approved Loki Image2 Banner V2 at the top of the GitHub README and reorganize the page so installation and first use appear before the image-only style gallery.

**Architecture:** This is a documentation-only change. One new PNG is copied into `docs/`, while `README.md` is reordered without modifying Skill behavior; validation checks local image references, sensitive data, the existing test suite, the Skill contract, and the remote branch hash.

**Tech Stack:** GitHub-flavored Markdown, HTML image/table elements, PNG assets, PowerShell, Git, Python standard-library unittest.

## Global Constraints

- Use the already approved Banner V2; do not regenerate or edit it.
- Keep the Banner at exact source dimensions `1672 × 941` and preserve its `16:9` composition.
- The 12-style gallery follows the confirmed “只展示图片” rule: no visible captions, prompts, news sources, or generation parameters.
- Do not modify files under `loki-image2/`.
- Do not publish local prompts, metadata, output directories, reference images, credentials, or machine-specific paths.
- Push only after all local verification commands pass.

---

### Task 1: Add the hero asset and reorganize README

**Files:**
- Create: `docs/loki-image2-banner-v2-16x9.png`
- Modify: `README.md`

**Interfaces:**
- Consumes: approved local Banner V2 at `output/loki-image2/20260809-165431-github-project-banner-v2/loki-image2-github-banner-v2-16x9.png`
- Produces: a README-relative asset path `docs/loki-image2-banner-v2-16x9.png` and working internal section links.

- [ ] **Step 1: Run the pre-change layout contract and verify it fails**

Run from the release repository:

```powershell
$readme = Get-Content .\README.md -Raw -Encoding UTF8
if (-not (Test-Path .\docs\loki-image2-banner-v2-16x9.png)) { throw 'banner missing' }
if ($readme.IndexOf('## 快速开始') -gt $readme.IndexOf('## 12 套视觉风格预览')) { throw 'quick start must precede gallery' }
```

Expected: failure with `banner missing` before implementation.

- [ ] **Step 2: Copy the approved Banner without overwriting unrelated assets**

Run:

```powershell
Copy-Item -LiteralPath 'G:\OPC\PJ\loki_image_skill_202608-08\output\loki-image2\20260809-165431-github-project-banner-v2\loki-image2-github-banner-v2-16x9.png' -Destination '.\docs\loki-image2-banner-v2-16x9.png'
```

- [ ] **Step 3: Reorder README with a focused hero and quick start**

Use `apply_patch` to make the top-level order exactly:

```text
# Loki Image2
<centered Banner image>
<one-sentence positioning>
<compact internal navigation>
## 快速开始
## 12 套视觉风格预览
## 主要能力
## 生成确认
## Provider
## 安全设计
## 测试
## 目录
```

The Banner element must be:

```html
<p align="center">
  <img src="docs/loki-image2-banner-v2-16x9.png" alt="Loki Image2" width="100%">
</p>
```

The quick-start code block must contain these three exact calls:

```text
$loki-image2 阅读这篇文章，帮我规划一张信息图
$loki-image2 使用 prompt 模式输出可复用的生图提示词
$loki-image2 使用 generate 模式直接生成图片
```

Pair the gallery images by similar proportions in this row order, with no visible captions:

```text
01 + 03
02 + 12
04 + 09
05 + 10
06 + 07
08 + 11
```

- [ ] **Step 4: Run the post-change layout and reference contract**

Run:

```powershell
$readme = Get-Content .\README.md -Raw -Encoding UTF8
if (-not (Test-Path .\docs\loki-image2-banner-v2-16x9.png)) { throw 'banner missing' }
if ($readme.IndexOf('## 快速开始') -gt $readme.IndexOf('## 12 套视觉风格预览')) { throw 'quick start must precede gallery' }
$missing = [regex]::Matches($readme, 'docs/[^"'']+\.png') | ForEach-Object { if (-not (Test-Path -LiteralPath $_.Value)) { $_.Value } }
if ($missing) { throw "missing README image: $missing" }
if ((Get-ChildItem .\docs\style-gallery -File -Filter '*.png').Count -ne 12) { throw 'gallery count mismatch' }
```

Expected: exit code `0`, no output.

- [ ] **Step 5: Commit the page implementation**

```powershell
git add -- README.md docs/loki-image2-banner-v2-16x9.png docs/superpowers/plans/2026-08-09-readme-hero-layout.md
git commit -m "docs: add project hero banner"
```

### Task 2: Run release verification

**Files:**
- Verify: `README.md`
- Verify: `docs/loki-image2-banner-v2-16x9.png`
- Verify: `docs/style-gallery/*.png`
- Verify: `loki-image2/tests/`

**Interfaces:**
- Consumes: Task 1 documentation commit.
- Produces: recorded exit-code evidence that the public release contains no detected secret-shaped values and remains behaviorally unchanged.

- [ ] **Step 1: Validate Markdown whitespace and staged state**

```powershell
git diff --check HEAD~1 HEAD
git status --short
```

Expected: `git diff --check` exit `0`; status has no output.

- [ ] **Step 2: Scan public text for secret-shaped values and machine-specific paths**

```powershell
$secretHits = rg -l --glob '!*.png' '(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|Bearer[ ]+[A-Za-z0-9._~+/-]{20,})' .
if ($LASTEXITCODE -eq 0 -and $secretHits) { throw 'secret-shaped value found' }
$pathHits = rg -n --glob '!*.png' 'G:\\OPC\\PJ|C:\\Users\\loki|H:\\loki' README.md CHANGELOG.md SECURITY.md loki-image2
if ($LASTEXITCODE -eq 0 -and $pathHits) { throw 'machine-specific public path found' }
```

Expected: exit code `0`, no findings.

- [ ] **Step 3: Verify PNG dimensions and metadata boundaries**

```powershell
Add-Type -AssemblyName System.Drawing
$path = '.\docs\loki-image2-banner-v2-16x9.png'
$img = [System.Drawing.Image]::FromFile($path)
try {
  if ($img.Width -ne 1672 -or $img.Height -ne 941) { throw 'unexpected banner dimensions' }
} finally { $img.Dispose() }
$bytes = [System.IO.File]::ReadAllBytes($path)
$ascii = [System.Text.Encoding]::ASCII.GetString($bytes)
if ($ascii.Contains('eXIf') -or $ascii.Contains('tEXt') -or $ascii.Contains('iTXt') -or $ascii.Contains('zTXt')) { throw 'public metadata chunk found' }
```

Expected: exit code `0`, no output.

- [ ] **Step 4: Run the complete offline tests**

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
python -m unittest discover -s .\loki-image2\tests -t .\loki-image2
```

Expected: `Ran 133 tests` and `OK`.

- [ ] **Step 5: Run the Skill validator in UTF-8 mode**

```powershell
$env:PYTHONUTF8='1'
$env:PYTHONDONTWRITEBYTECODE='1'
python 'H:\loki\.codex\skills\.system\skill-creator\scripts\quick_validate.py' .\loki-image2
```

Expected: `Skill is valid!` and exit code `0`.

### Task 3: Push and verify the remote branch

**Files:**
- No file changes.

**Interfaces:**
- Consumes: verified local commit from Tasks 1–2.
- Produces: remote `refs/heads/main` equal to local `HEAD`.

- [ ] **Step 1: Push the verified commit**

```powershell
git push origin main
```

Expected: `main -> main` with exit code `0`.

- [ ] **Step 2: Compare local and remote commit hashes**

```powershell
$local = git rev-parse HEAD
$remote = (git ls-remote origin refs/heads/main).Split("`t")[0]
if ($local -ne $remote) { throw "remote mismatch: local=$local remote=$remote" }
Write-Output $local
```

Expected: one 40-character hash and exit code `0`.

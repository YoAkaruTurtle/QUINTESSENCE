# 品牌资源与可复现构建（J11 / P6）

**结论：品牌资源**不**进仓库、不进交付包；仓库只保留"缺失时生成中性占位图"的构建脚本。这是有意策略，不是遗漏。**

---

## 1. 现状（代码事实）

| 对象 | 状态 | 依据 |
|---|---|---|
| `apps/desktop/src-tauri/icons/` | **被忽略** | `.gitignore:42` |
| `apps/desktop/public/brand.png` | **被忽略**（所有者自备素材放仓库之外） | `apps/desktop/public/.gitignore:2` |
| 图标生成 | `build.rs::ensure_icons()` | 仓库内构建脚本 |

`build.rs` 的规则：

1. 若 `32x32.png` / `128x128.png` / `128x128@2x.png` / `icon.ico` **四个都在** → **原样保留，不覆盖**；
2. 缺任一 → 生成一整套中性占位图（深色圆角块 + 青色环），保证干净检出也能编译；
3. 内容未变则**不重写文件**（避免 `tauri dev` 的文件监听死循环）。

**为什么这样设计：** 仓库的内容门禁不允许二进制文件；品牌素材属于所有者资产，不该进公开历史。

## 2. 换图标后"构建没变化"是怎么回事

`cargo build` 只在 `build.rs` 或输入变化时重跑。把新图标放进 `icons/` 而**不改任何被监听的输入**时，Cargo 会认为无需重建 —— 于是出现"0.88 秒完成、图标没变"。

正确步骤（任选其一）：

```powershell
# A. 最轻：让 build.rs 重跑
touch apps\desktop\src-tauri\build.rs      # 或任意改一行再改回

# B. 最稳：清掉该 crate 的构建产物
cargo clean -p quintessence-desktop
cargo build --release
```

**注意：** 新素材必须凑齐那**四个**文件，否则 `build.rs` 会认为不完整，重新生成占位图并**覆盖**你放进去的东西。这是最容易踩的一步。

## 3. 交付前必须验证

1. `cargo build --release` 成功（J11 实测：**2m43s**，产物 `apps/desktop/src-tauri/target/release/quintessence-desktop.exe`，**3.9 MB**）。
2. 图标与预期一致：**实际打开 exe 资源/安装后的快捷方式**看图标，不要只看文件时间戳。
3. 干净检出（无 `icons/`）也能构建：删掉 `icons/` 后 `cargo build --release` 应生成中性占位图并成功。
4. release 构建里**不含**所有者品牌素材的意外副本（`public/brand.png` 若存在会被打进前端产物，交付前须移除）。

## 4. 交付清单口径

- 品牌素材：**不随仓库交付**；需要时由所有者单独提供。
- 图标：构建期生成；**交付包里的图标必须来自正式构建**，不能来自 `cargo build --release` 之前的旧产物。
- 前端 `public/brand.png`：**交付前删除**，避免公开包携带未授权素材。

## 5. `cargo build --release` 不是可交付物

J11 实测：只跑 `cargo build --release` 会得到 `target/release/quintessence-desktop.exe`（3.9 MB），
但**直接启动它会报 `sidecar.spawn_failed / SIDECAR_NOT_FOUND`** —— 桌面壳需要旁边打包好的 Python sidecar。

- 这**不是**缺陷：sidecar 由 `apps/desktop/scripts/build_sidecar.ps1`（PyInstaller）单独产出；
- 完整顺序：`build_sidecar.ps1` → `build_app.ps1`（后者负责把 sidecar 放进资源目录并做 Tauri 打包）；
- 因此"能编译" ≠ "能安装/能启动"；交付前必须以**打包产物**做启动验证，不能用 `target/release` 的裸 exe。

本轮环境缺少 Lab PyInstaller venv（`build/c12b/venv`），所以打包与安装 smoke 记为**环境阻断**，
sidecar 侧的模型对话由 headless bridge smoke 覆盖（scripted 模型、无网络、无密钥）。

## 6. 已知限制

- 图标生成逻辑（`pixel()`）只画中性标记，**不是**最终品牌视觉。
- 未验证安装器（MSI/NSIS）阶段的图标嵌入；本轮只验证 `cargo build --release` 产物。
- 若将来把品牌素材纳入仓库，必须先解决内容门禁的二进制策略，而不是简单 `git add -f`。

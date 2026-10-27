# Mod 图标

当前 Mod 图标由脚本合成：原始透明「开心打游戏」角色图叠加游戏自带的 `images/ui.xml` 中 `portrait_bg.tex` 底板。米黄色渐变底和边框直接取自本机游戏资源，不再使用 imagegen 重绘的底板或改变人物配色。底板和边框均属于最终图标图像，游戏不会自动添加这层装饰。

- `modicon.png`：256 × 256 RGBA PNG 图标，包含纸质底板和黑金边框，可用于预览或重新导出纹理。
- `modicon.tex`：预乘 Alpha 的 RGBA KTEX 纹理，含 256 × 256 至 1 × 1 的完整 mipmap 层级，按游戏纹理坐标方向存储；各层均保留透明通道。
- `modicon.xml`：图集定义，裁去四边各 1 像素。
- `modinfo.lua`：通过 `icon_atlas` 和 `icon` 引用上述文件。
- `assets/logo-character.png`：用户确认的透明角色源图，保持原图颜色。
- `tools/build_modicon.py`：读取本机游戏底板、合成角色并生成 PNG、TEX 和 XML，不修改游戏安装目录。

## 重新生成

脚本需要 Pillow，仅作为图标制作工具依赖，不影响 Mod 和 MCP 运行。

```powershell
python -m pip install Pillow
python tools/build_modicon.py --game-dir "C:/Program Files (x86)/Steam/steamapps/common/Don't Starve Together"
```

默认输出 256 × 256，可用 `--size 128` 或 `--size 512` 调整。`--character` 可指定新的透明角色源图，`--output-dir` 可生成预览而不替换项目图标。底板从游戏自己的 `data/databundles/images.zip` 读取，保留 Klei 的原始图案与颜色。

替换图标后需要一并更新 PNG、TEX 和 XML，不能只将 PNG 重命名为 TEX。可使用 [KTools / ktech](https://github.com/nsimplex/ktools) 重新导出游戏纹理。图标引用格式见 [Klei 论坛图标说明](https://kleiforums.com/forums/topic/126518-guide-about-mod-icons/)。

形象参考来自 [yyh-001/dsh-meme：加班](https://github.com/yyh-001/dsh-meme/blob/main/memes/dafeiyu-001/memes/cpu/jiaban.jpg)。角色经内置图像编辑工具按用户要求改为开心操作电脑、将咖啡杯放在旁边；用户随后使用本地工具完成透明背景。原版底板素材归 Klei Entertainment 所有，制作脚本从本机游戏中提取。透明角色版本仍可单独用于视频角标等用途。

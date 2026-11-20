# kb/inbox —— 手动投喂目录

把要学给 orange 的文件复制进这个目录，它会在后台扫描入库，之后聊天时就能用上。

## 支持格式

| 格式 | 说明 |
|---|---|
| `.txt` `.md` `.csv` `.json` `.log` | 内置解析，自动识别 UTF-8 / GBK |
| `.docx` | 段落 + 表格 |
| `.pdf` | 文字型直接读；图片型（扫描件）需要 OCR |
| `.xlsx` | 按工作表 |
| `.pptx` | 按页，含备注 |
| 图片（png/jpg/…） | 必须 OCR |

## 命令行

```bash
# 扫一遍 inbox（平时后台会自动扫，这是手动触发）
python agent/kb.py scan

# 指定只给某个会话看
python agent/kb.py ingest "C:\path\讲义.pdf" --chat wxid_xxx

# 群聊里的图片型 PDF：必须显式加 --ocr 才会上传云端
python agent/kb.py ingest "C:\path\扫描件.pdf" --ocr

# 检索测试
python agent/kb.py search "关键词"

# 撤回一份资料（检索立即失效）
python agent/kb.py forget 12
```

## 注意

- 单文件默认上限 32 MB（`config.json` 的 `kb.max_file_mb`）。
- 同一份文件重复投递只会入库一次（按内容 sha1 去重）。
- 图片型 PDF 在**私聊**场景会自动走云端 OCR；群聊默认只标记 `needs_ocr`，要加 `--ocr`。
- 想彻底停掉知识库：`config.json` 里 `kb.enabled = false`，完全恢复原状。

# Journal RSS Aggregator

Zotero 订阅重复标记、标题译文缓存与已读归档见 [Journal RSS Memory](zotero-feed-memory/README.md)。这些个人状态只保存在本机，不随公开 RSS 发布。英文期刊的语言标记为 `en`，中文汇总为 `zh-CN`。

## 科研论文速递

仓库同时整合了原有的 arXiv 邮件收集和每日论文速递功能。两个公开订阅分别承担不同用途：

```text
https://fengziclassmate.github.io/journal-rss/research-papers.xml
https://fengziclassmate.github.io/journal-rss/research-daily.xml
```

- `research-papers.xml`：每篇论文一条，保留英文原题，适合在 Zotero 中逐篇阅读和翻译。
- `research-daily.xml`：每天一条，记录当天的精选论文以及每个来源的采集状态。
- `research-archive/`：每日 JSON 和 HTML 快照，不依赖 RSS 阅读器的保留期限。

科研速递通过 `research_rss.py` 生成。arXiv API 与 Crossref 不需要私密凭证；邮箱和 DeepSeek 为可选增强功能。若要启用它们，在 GitHub 仓库的 `Settings -> Secrets and variables -> Actions` 中配置：

```text
ARXIV_EMAIL_ADDRESS
ARXIV_EMAIL_AUTH_CODE
DEEPSEEK_API_KEY
CROSSREF_MAILTO
```

不配置邮箱 Secrets 时，程序仍会通过 arXiv API 收集论文；不配置 DeepSeek 时，使用边界安全的关键词评分，不会阻止 RSS 更新。邮件正文、邮箱地址和 Message-ID 不写入公开输出，状态文件只保存 Message-ID 的 SHA-256 哈希。

本地运行：

```bash
python research_rss.py
python research_rss.py --offline
```

第二条命令仅根据已有状态重建 RSS，不访问网络、邮箱或模型服务。研究方向、期刊列表和筛选阈值在 `research-config.json` 中配置。

这个目录里有一个可直接运行的 RSS 聚合脚本：

```bash
pip install -r requirements.txt
python journal_rss_aggregator.py --start-year 2020 --end-year 2026 --output feed.xml --feed-link https://你的域名/feed.xml
```

生成的 `feed.xml` 就是最终 RSS 文件。当前公开订阅地址设置为：

```text
https://fengziclassmate.github.io/journal-rss/feed.xml
```

单独期刊订阅地址：

```text
https://fengziclassmate.github.io/journal-rss/ijde.xml
https://fengziclassmate.github.io/journal-rss/pattern-recognition.xml
https://fengziclassmate.github.io/journal-rss/scs.xml
https://fengziclassmate.github.io/journal-rss/asc.xml
https://fengziclassmate.github.io/journal-rss/grsm-early-access.xml
https://fengziclassmate.github.io/journal-rss/tgrs-early-access.xml
https://fengziclassmate.github.io/journal-rss/tpami-early-access.xml
```

当前抓取规则：

- `https://www.dqxxkx.cn/CN/current`：官方 RSS XML 返回 404，所以从当期目录 HTML 中提取文章。
- `https://www.ygxb.ac.cn/rssList?lang=zh`：从前端接口发现 2020-2026 年的期次，再逐期拉官方 RSS。
- `https://ch.whu.edu.cn/showRssInfo.htm`：按 `/rss/{year}_{issue}.xml` 扫描 2020-2026 年 1-12 期，空期自动跳过。
- `https://www.tandfonline.com/journals/tjde20`：Taylor & Francis RSS 不稳定，改用 Crossref 按 ISSN `1753-8955` 拉取 *International Journal of Digital Earth* 从 `2026-06-01` 到运行当天的 journal article，单独输出到 `ijde.xml`。
- `https://www.sciencedirect.com/journal/pattern-recognition`：ScienceDirect 页面不直接抓取，改用 Crossref 按 ISSN `0031-3203` 拉取 *Pattern Recognition* 从 `2026-06-01` 到运行当天的 journal article，单独输出到 `pattern-recognition.xml`。
- `https://www.sciencedirect.com/journal/sustainable-cities-and-society`：ScienceDirect 页面不直接抓取，改用 Crossref 按 ISSN `2210-6707` 拉取 *Sustainable Cities and Society* 从 `2026-06-01` 到运行当天的 journal article，单独输出到 `scs.xml`。
- `https://www.sciencedirect.com/journal/applied-soft-computing`：ScienceDirect 页面不直接抓取，改用 Crossref 按 ISSN `1568-4946` 拉取 *Applied Soft Computing* 从 `2026-06-01` 到运行当天的 journal article，单独输出到 `asc.xml`。
- `https://ieeexplore.ieee.org/xpl/tocresult.jsp?isnumber=8976286`：IEEE Xplore 页面不稳定，改用 Crossref 按 ISSN `2168-6831` 拉取 *IEEE Geoscience and Remote Sensing Magazine* 从 `2026-02-01` 到运行当天的 Early Access journal article，单独输出到 `grsm-early-access.xml`。
- `https://ieeexplore.ieee.org/xpl/tocresult.jsp?isnumber=4358825`：IEEE Xplore 页面不稳定，改用 Crossref 按 ISSN `0196-2892` 拉取 *IEEE Transactions on Geoscience and Remote Sensing* 从 `2026-03-01` 到运行当天的 Early Access journal article，单独输出到 `tgrs-early-access.xml`。
- `https://ieeexplore.ieee.org/xpl/tocresult.jsp?isnumber=4359286`：IEEE Xplore 页面不稳定，改用 Crossref 按 ISSN `0162-8828` 拉取 *IEEE Transactions on Pattern Analysis and Machine Intelligence* 从 `2025-01-01` 到运行当天的 Early Access journal article，单独输出到 `tpami-early-access.xml`。

建议定时任务每天运行一次即可，不要高频抓取。

本仓库已包含 GitHub Actions 工作流 `.github/workflows/update-feed.yml`，默认每天北京时间 06:20 自动刷新并部署 GitHub Pages，也可以在 GitHub 的 Actions 页面手动运行。

如果 Pages 首次访问仍然是 404，请到仓库 `Settings -> Pages`，把 `Build and deployment -> Source` 设为 `GitHub Actions`。

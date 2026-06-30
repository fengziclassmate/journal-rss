# Journal RSS Aggregator

这个目录里有一个可直接运行的 RSS 聚合脚本：

```bash
pip install -r requirements.txt
python journal_rss_aggregator.py --start-year 2020 --end-year 2026 --output feed.xml --feed-link https://你的域名/feed.xml
```

生成的 `feed.xml` 就是最终 RSS 文件。当前公开订阅地址设置为：

```text
https://fengziclassmate.github.io/journal-rss/feed.xml
```

当前抓取规则：

- `https://www.dqxxkx.cn/CN/current`：官方 RSS XML 返回 404，所以从当期目录 HTML 中提取文章。
- `https://www.ygxb.ac.cn/rssList?lang=zh`：从前端接口发现 2020-2026 年的期次，再逐期拉官方 RSS。
- `https://ch.whu.edu.cn/showRssInfo.htm`：按 `/rss/{year}_{issue}.xml` 扫描 2020-2026 年 1-12 期，空期自动跳过。

建议定时任务每天运行一次即可，不要高频抓取。

本仓库已包含 GitHub Actions 工作流 `.github/workflows/update-feed.yml`，默认每天北京时间 06:20 自动刷新，也可以在 GitHub 的 Actions 页面手动运行。

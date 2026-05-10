#!/usr/bin/env node
/**
 * 从 stdin 读 HTML，用 @mozilla/readability 抽正文 + turndown 转 markdown，写到 stdout。
 *
 * 用法：cat page.html | node readability-converter.js > page.md
 *
 * 退出码：
 *   0  成功
 *   1  未捕获异常
 *   2  stdin 为空
 *   3  Readability 未能抽出主体内容
 *
 * CLAUDE.md 规则 25：fail-fast，失败必以非零退出码 + stderr 信息退出。
 */

const { JSDOM, VirtualConsole } = require("jsdom");
const { Readability } = require("@mozilla/readability");
const TurndownService = require("turndown");

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf-8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}

async function main() {
  const html = await readStdin();
  if (!html.trim()) {
    process.stderr.write("readability-converter: empty stdin\n");
    process.exit(2);
  }

  // jsdom 解析 HTML；VirtualConsole 屏蔽 CSS 解析告警，避免日志刷屏。
  const virtualConsole = new VirtualConsole();
  const dom = new JSDOM(html, { virtualConsole });
  const reader = new Readability(dom.window.document);
  const article = reader.parse();
  if (!article || !article.content) {
    process.stderr.write("readability-converter: Readability 未抽出主体内容\n");
    process.exit(3);
  }

  // article.content 是清洗后的 HTML 主体；用 turndown 转 markdown。
  const td = new TurndownService({
    headingStyle: "atx", // # 风格而非下划线
    codeBlockStyle: "fenced", // ``` 而非缩进
    bulletListMarker: "-",
    emDelimiter: "*",
  });
  const markdown = td.turndown(article.content);

  // 把标题以 H1 形式写在最前。
  const title = article.title ? `# ${article.title}\n\n` : "";
  process.stdout.write(title + markdown);
}

main().catch((err) => {
  process.stderr.write(
    `readability-converter: ${err.stack || err.message || err}\n`
  );
  process.exit(1);
});

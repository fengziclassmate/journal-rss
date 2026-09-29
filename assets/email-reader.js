'use strict';
(() => {
  const search = document.querySelector('#search');
  if (!search) return;
  const category = document.querySelector('#category');
  const abstracts = document.querySelector('#abstracts');
  const toc = document.querySelector('#toc');
  const count = document.querySelector('#count');
  const papers = [...document.querySelectorAll('#papers article')].map((node, index) => {
    node.id = `paper-${index + 1}`;
    const title = node.querySelector('h2');
    const paragraphs = [...node.querySelectorAll(':scope > p')];
    const categories = (paragraphs[1]?.textContent || '').split(',').map(x => x.trim()).filter(Boolean);
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = '摘要'; details.append(summary);
    if (paragraphs[2]) details.append(paragraphs[2]);
    node.append(details);
    const link = document.createElement('a');
    link.href = `#${node.id}`; link.textContent = `${index + 1}. ${title.textContent}`; toc.append(link);
    return {node, categories, details, link, text: node.textContent.toLocaleLowerCase()};
  });
  [...new Set(papers.flatMap(p => p.categories))].sort().forEach(name => {
    const option = document.createElement('option'); option.value = name; option.textContent = name; category.append(option);
  });
  function highlight(node, query) {
    node.querySelectorAll('mark').forEach(mark => mark.replaceWith(document.createTextNode(mark.textContent)));
    node.normalize();
    if (!query) return;
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    const texts = []; while (walker.nextNode()) texts.push(walker.currentNode);
    for (const text of texts) {
      const value = text.nodeValue; const lower = value.toLocaleLowerCase();
      let start = 0, match = lower.indexOf(query); if (match < 0) continue;
      const fragment = document.createDocumentFragment();
      while (match >= 0) {
        fragment.append(document.createTextNode(value.slice(start, match)));
        const mark = document.createElement('mark'); mark.textContent = value.slice(match, match + query.length); fragment.append(mark);
        start = match + query.length; match = lower.indexOf(query, start);
      }
      fragment.append(document.createTextNode(value.slice(start))); text.replaceWith(fragment);
    }
  }
  function filter() {
    const query = search.value.trim().toLocaleLowerCase(); let shown = 0;
    for (const paper of papers) {
      const visible = (!category.value || paper.categories.includes(category.value)) && (!query || paper.text.includes(query));
      paper.node.hidden = paper.link.hidden = !visible;
      if (visible) shown++;
      highlight(paper.node, visible ? query : '');
    }
    count.textContent = `显示 ${shown} / ${papers.length} 篇`;
  }
  let timer;
  search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(filter, 180); });
  category.addEventListener('change', filter);
  abstracts.addEventListener('change', () => papers.forEach(p => { p.details.open = abstracts.checked; }));
  document.querySelector('#filters').addEventListener('submit', e => e.preventDefault());
  filter();
})();

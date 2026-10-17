// Hand-drawn overview diagram for the README (docs/overview.zh.svg and docs/overview.en.svg).
// Sketchy shapes come from rough.js; text is turned into outlines with the LXGW WenKai font (SIL OFL 1.1,
// https://github.com/lxgw/LxgwWenKai), so it looks the same for every viewer. To regenerate, in a scratch folder:
//   npm install roughjs@4.6.6 opentype.js@1.3.4
//   node draw_overview.mjs <LXGWWenKai-Regular.ttf> <repo>/docs
import { writeFileSync } from 'node:fs';
import { join } from 'node:path';
import rough from 'roughjs/bundled/rough.esm.js';
import opentype from 'opentype.js';

const [fontPath, outDir] = process.argv.slice(2);
const font = opentype.loadSync(fontPath);
const W = 1100, H = 650;
const INK = '#1e1e1e', SOFT = '#5c5f66', BLUE = '#1971c2', GREEN = '#2f9e44';

const TEXT = {
  zh: {
    title: 'agent-fleet：一个 AI，指挥好几台电脑上的 AI',
    you: '你', bubble: ['让【经理cris】', '把今天的视频剪好'], say: '说一句话',
    orch: ['管理端', 'Codex 或 Claude Code', '+ agent-fleet 插件'],
    dispatch: '派任务', net: ['局域网', 'Tailscale'], report: '做完自动汇报',
    pc1: '电脑 1 · Windows', main1: '主 Agent「经理cris」', pc2: '电脑 2 · Mac', main2: '主 Agent「Mac 助手」',
    sub: '子 Agent', s1: '剪辑视频', s2: '发布视频', s3: '操作浏览器', s4: '整理文件',
    board: ['只读看板', '手机上随时看进度'],
    foot: '主 Agent 自己决定怎么拆任务 · 子 Agent 做完会叫醒它 · 新电脑用配对码接入',
  },
  en: {
    title: 'agent-fleet: one AI directs the AIs on many computers',
    you: 'You', bubble: ['"Have Manager Cris', 'edit today\'s video"'], say: 'just say it',
    orch: ['Orchestrator', 'Codex or Claude Code', '+ agent-fleet plugin'],
    dispatch: 'dispatch', net: ['LAN or', 'Tailscale'], report: 'reports back when done',
    pc1: 'Computer 1 · Windows', main1: 'Main agent "Manager Cris"', pc2: 'Computer 2 · Mac', main2: 'Main agent "Mac helper"',
    sub: 'Sub-agent', s1: 'edits video', s2: 'publishes it', s3: 'uses the browser', s4: 'sorts files',
    board: ['Read-only dashboard', 'check progress anywhere'],
    foot: 'The main agent splits the work · finished sub-agents wake it · new computers join with a pairing code',
  },
};

function draw(lang) {
  const t = TEXT[lang];
  const gen = rough.generator();
  let seed = 7;
  const parts = [];
  const opts = (extra = {}) => ({ seed: seed++, roughness: 1.3, bowing: 1.2, stroke: INK, strokeWidth: 1.7, ...extra });
  const add = (drawable, attrs = '') => {
    for (const p of gen.toPaths(drawable)) {
      parts.push(`<path d="${p.d}" stroke="${p.stroke}" stroke-width="${p.strokeWidth}" fill="${p.fill || 'none'}" ${attrs}/>`);
    }
  };
  const text = (s, x, y, size, { color = INK, anchor = 'middle' } = {}) => {
    const width = font.getAdvanceWidth(s, size);
    const left = anchor === 'middle' ? x - width / 2 : anchor === 'end' ? x - width : x;
    parts.push(`<path d="${font.getPath(s, left, y, size).toPathData(1)}" fill="${color}"/>`);
  };
  const box = (x, y, w, h, fill) => add(gen.rectangle(x, y, w, h, opts(fill ? { fill, fillStyle: 'hachure', hachureGap: 8, fillWeight: 1.1, hachureAngle: -41 } : {})));
  const arrow = (points, color = BLUE, dashed = false) => {
    const attrs = dashed ? 'stroke-dasharray="9 7"' : '';
    add(points.length > 2 ? gen.curve(points, opts({ stroke: color, strokeWidth: 2 })) : gen.line(...points[0], ...points[1], opts({ stroke: color, strokeWidth: 2 })), attrs);
    const [ax, ay] = points[points.length - 2], [bx, by] = points[points.length - 1];
    const angle = Math.atan2(by - ay, bx - ax);
    for (const turn of [0.45, -0.45]) {
      add(gen.line(bx, by, bx - 16 * Math.cos(angle + turn), by - 16 * Math.sin(angle + turn), opts({ stroke: color, strokeWidth: 2, roughness: 0.8 })));
    }
  };

  text(t.title, W / 2, 52, 32);

  // You, with what you say
  add(gen.circle(105, 170, 46, opts()));
  add(gen.line(105, 193, 105, 255, opts()));
  add(gen.line(105, 212, 75, 238, opts()));
  add(gen.line(105, 212, 138, 232, opts()));
  add(gen.line(105, 255, 82, 300, opts()));
  add(gen.line(105, 255, 128, 300, opts()));
  text(t.you, 105, 336, 26);
  box(18, 360, 222, 92);
  add(gen.line(110, 360, 100, 342, opts({ roughness: 0.6 })));
  text(t.bubble[0], 129, 397, lang === 'zh' ? 21 : 18);
  text(t.bubble[1], 129, 428, lang === 'zh' ? 21 : 18);
  arrow([[165, 225], [298, 228]]);
  text(t.say, 232, 212, 17, { color: SOFT });

  // Orchestrator
  box(305, 150, 250, 172, '#a5d8ff');
  text(t.orch[0], 430, 200, 28);
  text(t.orch[1], 430, 248, 19);
  text(t.orch[2], 430, 284, 19);

  // Computers
  const computer = (y, fill, title, main, a, b) => {
    box(665, y, 410, 222, fill);
    text(title, 870, y + 34, 18, { color: SOFT });
    text(main, 870, y + 72, lang === 'zh' ? 24 : 21);
    for (const [x, label] of [[688, a], [880, b]]) {
      box(x, y + 112, 172, 84, '#ffffff');
      add(gen.line(870, y + 84, x + 86, y + 110, opts({ strokeWidth: 1.3, roughness: 0.9 })));
      text(t.sub, x + 86, y + 146, 17, { color: SOFT });
      text(label, x + 86, y + 178, 20);
    }
  };
  computer(92, '#ffec99', t.pc1, t.main1, t.s1, t.s2);
  computer(352, '#b2f2bb', t.pc2, t.main2, t.s3, t.s4);
  arrow([[560, 205], [660, 178]]);
  arrow([[560, 275], [660, 440]]);
  text(t.dispatch, 612, 248, 19, { color: BLUE });
  text(t.net[0], 612, 280, 15, { color: SOFT });
  text(t.net[1], 612, 300, 15, { color: SOFT });
  arrow([[668, 104], [560, 66], [452, 142]], GREEN, true);
  if (lang === 'zh') text(t.report, 565, 112, 17, { color: GREEN });
  else text(t.report, 650, 122, 14, { color: GREEN, anchor: 'end' });

  // Dashboard on a phone
  add(gen.rectangle(318, 410, 66, 112, opts()));
  add(gen.rectangle(328, 424, 46, 76, opts({ fill: '#e7f5ff', fillStyle: 'solid', roughness: 0.7 })));
  add(gen.circle(351, 511, 8, opts({ roughness: 0.6 })));
  arrow([[660, 500], [400, 470]], SOFT, true);
  text(t.board[0], 405, 552, 20, { anchor: 'start' });
  text(t.board[1], 405, 580, 17, { anchor: 'start', color: SOFT });

  text(t.foot, W / 2, 628, lang === 'zh' ? 19 : 16, { color: SOFT });

  const label = lang === 'zh' ? 'agent-fleet 图解：你对管理端说一句话，它把任务派给各台电脑的主 Agent，主 Agent 再派子 Agent 干活，做完自动汇报'
    : 'agent-fleet overview: you tell the orchestrator one sentence, it dispatches to each computer\'s main agent, which sends sub-agents to do the work and reports back';
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${label}">
<!-- Generated by tools/draw_overview.mjs; text outlines from LXGW WenKai (SIL OFL 1.1). -->
<rect x="0" y="0" width="${W}" height="${H}" rx="18" fill="#fffdf8"/>
<g stroke-linecap="round" stroke-linejoin="round">
${parts.join('\n')}
</g>
</svg>
`;
}

for (const lang of ['zh', 'en']) writeFileSync(join(outDir, `overview.${lang}.svg`), draw(lang));
console.log('ok');

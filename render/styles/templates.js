/* The 35 post templates. Each takes a context c and returns the post's inner HTML:
   c = {kicker, head, body, detail, handle, mark, name, img, imgs, land, ratio, slide}.
   Units are cqmin (container query min), so one design fits 1:1, 4:5, 9:16 and 1.91:1.
   Colours and fonts arrive as CSS variables (--bg --fg --paper --ink --ac --ac2 --mut, the contrast-checked
   --ac-t --ac-p --ac-d --mut-t --mut-p --on-ac --on-ac2 --on-bg --night, and --f-disp --f-serif --f-sans
   --f-mono --f-hand), set by engine.js from the brand kit. Text goes through esc()/br() from engine.js;
   images are data: URLs checked by server.py. Metadata (name, kind, own palette...) is in styles.json. */
"use strict";
window.TEMPLATES = {
"editorial": c=>`<div class="abs" style="padding:9cqmin;display:flex;flex-direction:column;background:var(--paper);color:var(--ink)">
  <div style="display:flex;justify-content:space-between;gap:3cqmin;border-bottom:.3cqmin solid currentColor;padding-bottom:2cqmin" class="kick"><span>${esc(c.kicker)}</span><span>${esc(c.detail)}</span></div>
  <div style="flex:1;display:flex;flex-direction:column;justify-content:center;min-height:0">
   <h2 style="font:500 10.5cqmin/1.04 var(--f-serif);letter-spacing:-.02em">${br(c.head)}</h2>
   <p style="margin-top:5cqmin;font:italic 400 3.6cqmin/1.4 var(--f-serif);max-width:82%;color:var(--mut-p)">${br(c.body)}</p></div>
  <div style="border-top:.15cqmin solid currentColor;padding-top:2cqmin" class="handle">${esc(c.handle)}</div></div>`,

"swiss": c=>`<div class="abs" style="display:grid;grid-template-columns:1fr 2fr;grid-template-rows:auto 1fr auto auto;background:var(--paper);color:var(--ink)">
  <div style="grid-column:1/3;background:var(--ac);color:var(--on-ac);padding:5cqmin 6cqmin 9cqmin" class="kick">${esc(c.kicker)}</div>
  <div style="grid-column:1/3;padding:6cqmin;align-self:end;min-height:0"><h2 style="font:800 11cqmin/.98 var(--f-sans);letter-spacing:-.045em">${br(c.head)}</h2></div>
  <p style="grid-column:2/3;padding:0 6cqmin 4cqmin 0;font:400 3.4cqmin/1.38 var(--f-sans)">${br(c.body)}</p>
  <div style="grid-column:1/3;padding:0 6cqmin 6cqmin;font:800 3cqmin/1 var(--f-sans)">${esc(c.handle)}</div></div>`,

"noir": c=>`<div class="abs" style="padding:10cqmin;display:flex;flex-direction:column;justify-content:space-between">
  <div class="kick" style="color:var(--ac-t);max-width:70%">${esc(c.kicker)}</div>
  <div><h2 style="font:400 9cqmin/1.1 var(--f-serif);letter-spacing:-.01em">${br(c.head)}</h2>
   <div style="width:12cqmin;height:.3cqmin;background:var(--ac);margin:6cqmin 0 4cqmin"></div>
   <p style="font:300 3.5cqmin/1.5 var(--f-sans);color:var(--mut-t);max-width:80%">${br(c.body)}</p></div>
  <div class="handle" style="color:var(--mut-t)">${esc(c.handle)}</div></div>`,

"bigquote": c=>`<div class="abs" style="padding:9cqmin;display:flex;flex-direction:column;justify-content:center">
  <div style="font:700 40cqmin/.6 var(--f-serif);color:var(--ac-t);height:19cqmin;margin-left:-2cqmin" aria-hidden="true">“</div>
  <h2 style="font:italic 500 8.4cqmin/1.14 var(--f-serif)">${br(c.head)}</h2>
  <p style="margin-top:6cqmin;font:600 3cqmin/1.3 var(--f-sans);letter-spacing:.14em;text-transform:uppercase;color:var(--ac-t)">— ${esc(c.mark)}</p></div>`,

"poster": c=>{const w=words(c.head).slice(0,8);const L=Math.max(...w.map(x=>x.length),3);const fs=Math.min(19,120/L,92/w.length);
 return `<div class="abs" style="padding:5cqmin;display:flex;flex-direction:column;justify-content:space-between;gap:3cqmin;background:var(--ac2);color:var(--on-ac2)">
  <div class="kick" style="display:flex;justify-content:space-between;gap:3cqmin"><span>${esc(c.kicker)}</span><span>${esc(c.detail)}</span></div>
  <h2 style="font:400 ${fs}cqmin/.86 var(--f-disp);text-transform:uppercase;letter-spacing:-.01em">${w.map((x,i)=>`<div style="${i%2?`color:transparent;-webkit-text-stroke:.4cqmin var(--on-ac2)`:""}">${esc(x)}</div>`).join("")}</h2>
  <div style="display:flex;justify-content:space-between;font:700 3.2cqmin/1 var(--f-sans);text-transform:uppercase"><span>${esc(c.handle)}</span><span aria-hidden="true">→</span></div></div>`},

"brutal": c=>`<div class="abs" style="padding:5cqmin;display:flex;flex-direction:column;gap:3cqmin;background:var(--paper);color:var(--ink);font-family:var(--f-mono)">
  <div style="border:.5cqmin solid currentColor;padding:2.5cqmin 3cqmin;font:700 3cqmin/1.2 var(--f-mono);text-transform:uppercase;display:flex;justify-content:space-between;gap:3cqmin"><span>${esc(c.kicker)}</span><span>${esc(c.detail)}</span></div>
  <div style="border:.5cqmin solid currentColor;padding:4cqmin;flex:1;min-height:0;display:flex;align-items:flex-end;background:var(--bg);color:var(--on-bg)"><h2 style="font:700 8.5cqmin/1.04 var(--f-mono);letter-spacing:-.04em">${br(c.head)}</h2></div>
  <div style="border:.5cqmin solid currentColor;padding:3cqmin;font:400 3.2cqmin/1.4 var(--f-mono)">&gt; ${br(c.body)}</div>
  <div style="font:700 3cqmin/1 var(--f-mono);background:var(--ac);color:var(--on-ac);padding:2.2cqmin 3cqmin;align-self:flex-start">${esc(c.handle)}</div></div>`,

"gradient": c=>`<div class="abs" style="background:radial-gradient(60% 50% at 20% 15%,color-mix(in srgb,var(--ac) 80%,transparent),transparent 70%),radial-gradient(55% 45% at 85% 85%,color-mix(in srgb,var(--ac2) 75%,transparent),transparent 70%),var(--bg)"></div>
  <div class="abs" style="padding:9cqmin;display:flex;flex-direction:column;justify-content:center;text-align:center;align-items:center;color:var(--on-bg)">
  <div class="kick" style="background:color-mix(in srgb,var(--bg) 55%,transparent);padding:1.6cqmin 3cqmin;border-radius:99cqmin">${esc(c.kicker)}</div>
  <h2 style="margin-top:6cqmin;font:700 9.6cqmin/1.04 var(--f-disp);letter-spacing:-.035em;text-shadow:0 .3cqmin 2cqmin color-mix(in srgb,var(--bg) 60%,transparent)">${br(c.head)}</h2>
  <p style="margin-top:4.5cqmin;font:400 3.6cqmin/1.45 var(--f-sans);max-width:80%;text-shadow:0 .2cqmin 1.5cqmin var(--bg)">${br(c.body)}</p>
  <div class="handle" style="margin-top:6cqmin">${esc(c.handle)}</div></div>`,

"newspaper": c=>{const long=String(c.body).length>140;
 return `<div class="abs" style="background:var(--paper);color:var(--ink);padding:6cqmin;display:flex;flex-direction:column;font-family:var(--f-serif)">
  <div style="text-align:center;border-bottom:.8cqmin double currentColor;padding-bottom:1.5cqmin"><div style="font:700 8cqmin/1.05 var(--f-serif)">${esc(c.mark)}</div>
   <div style="display:flex;justify-content:space-between;gap:2cqmin;font:400 2.3cqmin/1.2 var(--f-sans);margin-top:2cqmin;border-top:.2cqmin solid currentColor;padding-top:1.2cqmin;text-transform:uppercase;letter-spacing:.08em"><span>${esc(c.kicker)}</span><span>${esc(c.detail)}</span></div></div>
  <h2 style="margin:5cqmin 0 3cqmin;font:700 8.6cqmin/1.04 var(--f-serif);text-align:center">${br(c.head)}</h2>
  <div style="${long?"columns:2;column-gap:4cqmin;column-rule:.2cqmin solid currentColor;":"max-width:86%;margin:0 auto;"}font:400 3.4cqmin/1.45 var(--f-serif);text-align:${long?"justify":"center"};flex:1;min-height:0">${esc(c.body)}</div>
  <div style="border-top:.2cqmin solid currentColor;padding-top:1.5cqmin;font:italic 400 2.6cqmin/1.1 var(--f-serif);text-align:center">${esc(c.handle)}</div></div>`},

"quotecard": c=>`<div class="abs" style="display:flex;align-items:center;justify-content:center;padding:7cqmin;background:var(--ac)">
  <div style="background:var(--paper);color:var(--ink);border-radius:4cqmin;padding:6cqmin;width:100%;max-width:120cqmin;box-shadow:0 2cqmin 6cqmin rgba(0,0,0,.18)">
   <div style="display:flex;gap:3cqmin;align-items:center"><div style="width:11cqmin;height:11cqmin;border-radius:50%;background:var(--ac);display:grid;place-items:center;color:var(--on-ac);font:700 3.4cqmin/1 var(--f-disp);flex:none">${esc(String(c.mark).split(/\s+/).map(x=>x[0]).join("").slice(0,3))}</div>
    <div><div style="font:700 3.8cqmin/1.2 var(--f-sans)">${esc(c.name)}</div><div style="font:400 3.2cqmin/1.2 var(--f-sans);color:var(--mut-p)">${esc(c.handle)}</div></div></div>
   <h2 style="margin-top:4cqmin;font:500 5.6cqmin/1.3 var(--f-sans)">${br(c.head)}</h2>
   <p style="margin-top:3cqmin;font:400 3.8cqmin/1.4 var(--f-sans);color:var(--mut-p)">${br(c.body)}</p></div></div>`,

"notes": c=>`<div class="abs" style="background:var(--paper);color:var(--ink);background-image:repeating-linear-gradient(to bottom,transparent 0 7.4cqmin,color-mix(in srgb,var(--ac2) 45%,transparent) 7.4cqmin 7.6cqmin);padding:10cqmin 8cqmin 8cqmin 14cqmin;display:flex;flex-direction:column">
  <div style="position:absolute;left:10cqmin;top:0;bottom:0;width:.3cqmin;background:var(--ac);opacity:.6"></div>
  <div style="font:700 4.6cqmin/7.5cqmin var(--f-hand);color:var(--ac-p)">${esc(c.kicker)}</div>
  <h2 style="font:700 8.4cqmin/7.5cqmin var(--f-hand);margin-top:7.5cqmin">${br(c.head)}</h2>
  <div style="font:500 5cqmin/7.5cqmin var(--f-hand);margin-top:7.5cqmin">${br(c.body)}</div>
  <div style="margin-top:auto;align-self:flex-end;font:700 4.6cqmin/1 var(--f-hand);transform:rotate(-4deg)">— ${esc(c.handle)}</div></div>`,

"ticket": c=>`<div class="abs" style="background:var(--bg);display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4cqmin;padding:7cqmin">
  <div style="position:relative;width:100%;max-width:130cqmin;background:var(--paper);color:var(--ink);border-radius:3cqmin;display:grid;grid-template-columns:1fr 22cqmin;overflow:hidden">
   <div style="padding:6cqmin;border-right:.5cqmin dashed color-mix(in srgb,var(--ink) 40%,transparent);min-width:0">
    <div class="kick" style="color:var(--ac-p)">${esc(c.kicker)}</div>
    <h2 style="margin-top:3cqmin;font:400 9cqmin/.98 var(--f-disp);text-transform:uppercase">${br(c.head)}</h2>
    <p style="margin-top:3cqmin;font:400 3.1cqmin/1.35 var(--f-sans);color:var(--mut-p)">${br(c.body)}</p></div>
   <div style="padding:5cqmin 2cqmin;display:flex;flex-direction:column;justify-content:space-between;align-items:center;gap:3cqmin;background:var(--ac);color:var(--on-ac)">
    <div style="font:700 2.8cqmin/1 var(--f-mono);writing-mode:vertical-rl;letter-spacing:.2em">ADMIT ONE</div>
    <div style="font:700 2.4cqmin/1.2 var(--f-mono);text-align:center;writing-mode:vertical-rl">${esc(c.detail)}</div></div>
   <div style="position:absolute;right:19cqmin;top:-3cqmin;width:6cqmin;height:6cqmin;border-radius:50%;background:var(--bg)"></div>
   <div style="position:absolute;right:19cqmin;bottom:-3cqmin;width:6cqmin;height:6cqmin;border-radius:50%;background:var(--bg)"></div></div>
  <div class="handle" style="color:var(--on-bg)">${esc(c.handle)}</div></div>`,

"list": c=>{const it=String(c.body).split(/(?<=[.;])\s+/).map(s=>s.trim()).filter(Boolean);const items=(it.length>=2?it:[c.body,"Who it's for","How to start"]).slice(0,3);
 return `<div class="abs" style="padding:8cqmin;display:flex;flex-direction:column;background:var(--paper);color:var(--ink)">
  <div class="kick" style="color:var(--ac-p)">${esc(c.kicker)}</div>
  <h2 style="margin:4cqmin 0 5cqmin;font:700 8cqmin/1.04 var(--f-disp);letter-spacing:-.025em">${br(c.head)}</h2>
  ${items.map((x,i)=>`<div style="display:flex;gap:4cqmin;align-items:baseline;border-top:.25cqmin solid color-mix(in srgb,var(--ink) 20%,transparent);padding:2.6cqmin 0"><span style="font:700 5.5cqmin/1 var(--f-mono);color:var(--ac-p)">0${i+1}</span><span style="font:500 3.8cqmin/1.3 var(--f-sans)">${esc(x)}</span></div>`).join("")}
  <div style="margin-top:auto;display:flex;justify-content:space-between;gap:3cqmin;padding-top:3cqmin" class="handle"><span>${esc(c.handle)}</span><span>Save for later</span></div></div>`},

"neon": c=>`<div class="abs" style="padding:8cqmin;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;
   background-image:linear-gradient(90deg,rgba(255,255,255,.03) 1px,transparent 1px),linear-gradient(rgba(255,255,255,.03) 1px,transparent 1px);background-size:6cqmin 6cqmin;background-color:var(--night)">
  <div style="font:400 4.4cqmin/1.2 var(--f-disp);color:var(--ac2-d);text-shadow:0 0 1cqmin var(--ac2),0 0 4cqmin var(--ac2)">${esc(c.kicker)}</div>
  <h2 style="margin:6cqmin 0;font:400 9.4cqmin/1.12 var(--f-hand);color:#fff;text-shadow:0 0 .6cqmin #fff,0 0 2cqmin var(--ac),0 0 5cqmin var(--ac),0 0 9cqmin var(--ac)">${br(c.head)}</h2>
  <p style="font:400 3.4cqmin/1.4 var(--f-sans);color:rgba(255,255,255,.78);max-width:78%">${br(c.body)}</p>
  <div class="handle" style="margin-top:6cqmin;color:rgba(255,255,255,.7)">${esc(c.handle)}</div></div>`,

"deco": c=>`<div class="abs" style="padding:5cqmin">
  <div class="abs" style="inset:4cqmin;border:.4cqmin solid var(--ac)"></div><div class="abs" style="inset:5.6cqmin;border:.15cqmin solid var(--ac)"></div>
  ${["top:4cqmin;left:4cqmin","top:4cqmin;right:4cqmin","bottom:4cqmin;left:4cqmin","bottom:4cqmin;right:4cqmin"].map(p=>`<div style="position:absolute;${p};width:9cqmin;height:9cqmin;border:.4cqmin solid var(--ac);background:var(--bg);transform:rotate(45deg) scale(.6)"></div>`).join("")}
  <div class="abs" style="inset:12cqmin;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center">
   <div style="font:400 3cqmin/1.2 var(--f-disp);letter-spacing:.25em;text-transform:uppercase;color:var(--ac-t)">${esc(c.kicker)}</div>
   <div style="display:flex;align-items:center;gap:2cqmin;margin:4cqmin 0;color:var(--ac)" aria-hidden="true"><span style="width:12cqmin;height:.25cqmin;background:currentColor"></span>◆<span style="width:12cqmin;height:.25cqmin;background:currentColor"></span></div>
   <h2 style="font:400 8.6cqmin/1.08 var(--f-disp);text-transform:uppercase;letter-spacing:.02em">${br(c.head)}</h2>
   <p style="margin-top:5cqmin;font:italic 400 3.6cqmin/1.4 var(--f-serif);color:var(--mut-t)">${br(c.body)}</p>
   <div style="margin-top:5cqmin;font:400 2.6cqmin/1.1 var(--f-sans);letter-spacing:.2em;text-transform:uppercase;color:var(--ac-t)">${esc(c.handle)}</div></div></div>`,

"terminal": c=>`<div class="abs" style="padding:7cqmin;display:flex;align-items:center;justify-content:center">
  <div style="width:100%;max-width:130cqmin;border:.25cqmin solid color-mix(in srgb,var(--fg) 20%,transparent);border-radius:2cqmin;overflow:hidden;background:color-mix(in srgb,var(--fg) 4%,var(--bg))">
   <div style="display:flex;gap:1.6cqmin;padding:2.4cqmin 3cqmin;border-bottom:.25cqmin solid color-mix(in srgb,var(--fg) 15%,transparent)"><i style="width:2.4cqmin;height:2.4cqmin;border-radius:50%;background:#ff5f57"></i><i style="width:2.4cqmin;height:2.4cqmin;border-radius:50%;background:#febc2e"></i><i style="width:2.4cqmin;height:2.4cqmin;border-radius:50%;background:#28c840"></i><span style="margin-left:auto;font:400 2.4cqmin/1 var(--f-mono);color:var(--mut-t)">${esc(c.handle)}</span></div>
   <div style="padding:5cqmin;font:400 3.4cqmin/1.6 var(--f-mono)">
    <div style="color:var(--mut-t)"># ${esc(c.kicker)}</div>
    <div><span style="color:var(--ac-t)">$</span> cat ${esc(String(c.detail).toLowerCase().replace(/\s+/g,"-"))}.md</div>
    <h2 style="font:700 7cqmin/1.18 var(--f-mono);margin:3cqmin 0;letter-spacing:-.03em">${br(c.head)}</h2>
    <div style="color:var(--mut-t)">${br(c.body)}</div>
    <div><span style="color:var(--ac-t)">$</span> <span style="background:var(--ac);color:var(--on-ac)">&nbsp;</span></div></div></div></div>`,

"stat": c=>{const m=String(c.detail).match(/^\s*([\d.,%+$]+)\s*(.*)$/);const big=m?m[1]:c.detail;const unit=m?m[2]:"";
 return `<div class="abs" style="padding:8cqmin;display:flex;flex-direction:column;justify-content:space-between">
  <div class="kick" style="color:var(--ac-t);max-width:72%">${esc(c.kicker)}</div>
  <div style="min-height:0"><h2 style="font:800 ${Math.min(44,150/Math.max(String(big).length,1))}cqmin/.86 var(--f-disp);letter-spacing:-.05em;color:var(--ac-t)">${esc(big)}</h2>
   ${unit?`<div style="font:600 4cqmin/1.1 var(--f-sans);text-transform:uppercase;letter-spacing:.12em;color:var(--ac-t);margin-top:2cqmin">${esc(unit)}</div>`:""}
   <div style="margin-top:4cqmin;font:600 6cqmin/1.12 var(--f-disp);letter-spacing:-.02em">${br(c.head)}</div></div>
  <div class="handle" style="color:var(--mut-t)">${esc(c.handle)}</div></div>`},

"event": c=>`<div class="abs" style="padding:8cqmin;display:flex;flex-direction:column;gap:5cqmin">
  <div class="kick" style="color:var(--ac-t);max-width:72%">${esc(c.kicker)}</div>
  <div style="display:flex;gap:5cqmin;align-items:stretch;flex:1;min-height:0">
   <div style="width:.8cqmin;background:var(--ac);flex:none"></div>
   <div style="display:flex;flex-direction:column;justify-content:center;min-width:0">
    <div style="font:900 9cqmin/1 var(--f-disp);text-transform:uppercase;color:var(--ac-t)">${esc(c.detail)}</div>
    <h2 style="margin-top:4cqmin;font:700 8cqmin/1.04 var(--f-disp);text-transform:uppercase;letter-spacing:-.01em">${br(c.head)}</h2>
    <p style="margin-top:4cqmin;font:400 3.5cqmin/1.45 var(--f-sans);color:var(--mut-t)">${br(c.body)}</p></div></div>
  <div style="display:flex;justify-content:space-between;align-items:center;gap:3cqmin;border-top:.25cqmin solid color-mix(in srgb,var(--fg) 25%,transparent);padding-top:3cqmin">
   <span class="handle">${esc(c.handle)}</span><span style="font:700 2.8cqmin/1 var(--f-sans);background:var(--ac);color:var(--on-ac);padding:1.8cqmin 3cqmin;border-radius:99cqmin;text-transform:uppercase;letter-spacing:.1em;white-space:nowrap">Save the date</span></div></div>`,

"carousel": c=>{const s=c.slide||0;const foot=`<div style="display:flex;justify-content:space-between;gap:3cqmin" class="handle"><span>${esc(c.handle)}</span><span>${s+1} / 3${s<2?" →":""}</span></div>`;
 if(s===0)return `<div class="abs" style="padding:8cqmin;display:flex;flex-direction:column;justify-content:space-between;background:var(--paper);color:var(--ink)">
  <div class="kick" style="color:var(--ac-p)">${esc(c.kicker)}</div>
  <h2 style="font:700 11cqmin/1 var(--f-disp);letter-spacing:-.04em">${br(c.head)}</h2>${foot}</div>`;
 if(s===1)return `<div class="abs" style="padding:8cqmin;display:flex;flex-direction:column;justify-content:space-between;background:var(--paper);color:var(--ink)">
  <div style="font:700 16cqmin/1 var(--f-mono);color:var(--ac-p)">01</div>
  <h2 style="font:500 6.4cqmin/1.25 var(--f-sans)">${br(c.body)}</h2>${foot}</div>`;
 return `<div class="abs" style="padding:8cqmin;display:flex;flex-direction:column;justify-content:space-between;background:var(--ac);color:var(--on-ac)">
  <div class="kick">${esc(c.kicker)}</div>
  <div><div style="font:800 6cqmin/1 var(--f-disp);text-transform:uppercase;letter-spacing:.04em">${esc(c.mark)}</div>
   <h2 style="margin-top:4cqmin;font:700 8cqmin/1.06 var(--f-disp);letter-spacing:-.03em;overflow-wrap:anywhere">${esc(c.handle)}</h2></div>
  <div class="handle">${s+1} / 3</div></div>`},

"scrim": c=>`<img class="bg" src="${c.img}" alt=""><div class="abs" style="background:linear-gradient(to top,rgba(0,0,0,.88) 0%,rgba(0,0,0,.4) 42%,transparent 68%)"></div>
  <div class="abs" style="padding:7cqmin;display:flex;flex-direction:column;justify-content:flex-end;color:#fff">
   <div class="kick" style="color:var(--ac-d)">${esc(c.kicker)}</div>
   <h2 style="margin-top:2.5cqmin;font:700 8.2cqmin/1.06 var(--f-disp);letter-spacing:-.02em">${br(c.head)}</h2>
   <p style="margin-top:2.5cqmin;font:400 3.4cqmin/1.4 var(--f-sans);max-width:88%">${br(c.body)}</p>
   <div class="handle" style="margin-top:3cqmin;opacity:.9">${esc(c.handle)}</div></div>`,

"lowerthird": c=>`<img class="bg" src="${c.img}" alt="">
  <div style="position:absolute;left:0;right:0;bottom:0;background:var(--ac);color:var(--on-ac);padding:5cqmin 6cqmin 6cqmin">
   <div style="position:absolute;bottom:100%;left:6cqmin;max-width:calc(100% - 12cqmin);background:var(--ac2);color:var(--on-ac2);padding:1.6cqmin 3cqmin;transform:translateY(40%)" class="kick">${esc(c.kicker)}</div>
   <h2 style="margin-top:1.5cqmin;font:800 7cqmin/1.06 var(--f-disp);letter-spacing:-.02em">${br(c.head)}</h2>
   <div style="display:flex;justify-content:space-between;align-items:flex-end;gap:4cqmin;margin-top:2.5cqmin;font:400 3.1cqmin/1.35 var(--f-sans)"><span style="max-width:70%">${br(c.body)}</span><span class="handle">${esc(c.handle)}</span></div></div>`,

"polaroid": c=>{const w=c.ratio<.95?72:c.land?52:58;
 return `<div class="abs" style="background:var(--paper);background-image:radial-gradient(color-mix(in srgb,var(--ink) 10%,transparent) .3cqmin,transparent .4cqmin);background-size:3cqmin 3cqmin"></div>
  <div class="abs" style="display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4cqmin;padding:6cqmin">
   <div style="position:relative;width:${w}cqmin;background:#fff;padding:3.5cqmin 3.5cqmin 0;box-shadow:0 2cqmin 5cqmin rgba(0,0,0,.25);transform:rotate(-2.5deg)">
    <div style="position:absolute;top:-2.5cqmin;left:50%;margin-left:-8cqmin;width:16cqmin;height:5cqmin;background:color-mix(in srgb,var(--ac) 55%,#fff);opacity:.85;transform:rotate(4deg)"></div>
    <div style="aspect-ratio:1;overflow:hidden"><img src="${c.img}" alt="" style="width:100%;height:100%;object-fit:cover;display:block"></div>
    <h2 style="font:700 ${(w/10).toFixed(1)}cqmin/1.1 var(--f-hand);color:#222;padding:3cqmin 0 4cqmin;text-align:center">${br(c.head)}</h2></div>
   <div style="text-align:center;color:var(--ink);max-width:90%"><div style="font:500 3.8cqmin/1.3 var(--f-hand)">${br(c.body)}</div><div class="handle" style="margin-top:1.6cqmin">${esc(c.handle)}</div></div></div>`},

"split": c=>`<div class="abs" style="display:grid;${c.land?"grid-template-columns:50% 50%":"grid-template-rows:54% 46%"}">
  <div style="position:relative;overflow:hidden"><img class="bg" src="${c.img}" alt=""></div>
  <div style="background:var(--bg);color:var(--fg);padding:6cqmin 7cqmin;display:flex;flex-direction:column;min-height:0">
   <div class="kick" style="color:var(--ac-t)">${esc(c.kicker)}</div>
   <h2 style="margin-top:2.5cqmin;font:600 7cqmin/1.08 var(--f-serif);letter-spacing:-.015em">${br(c.head)}</h2>
   <p style="margin-top:2.5cqmin;font:400 3.3cqmin/1.4 var(--f-sans);color:var(--mut-t)">${br(c.body)}</p>
   <div class="handle" style="margin-top:auto;padding-top:2cqmin">${esc(c.handle)} →</div></div></div>`,

"duotone": c=>`<img class="bg" src="${c.img}" alt="" style="filter:grayscale(1) contrast(1.15)"><div class="abs" style="background:var(--bg);mix-blend-mode:multiply"></div><div class="abs" style="background:var(--ac);mix-blend-mode:screen;opacity:.5"></div><div class="abs" style="background:linear-gradient(transparent 40%,rgba(0,0,0,.45))"></div>
  <div class="abs" style="padding:7cqmin;display:flex;flex-direction:column;justify-content:space-between;color:#fff">
   <div class="kick">${esc(c.kicker)}</div>
   <h2 style="font:400 14cqmin/.9 var(--f-disp);text-transform:uppercase;letter-spacing:-.01em;text-shadow:0 .3cqmin 2cqmin rgba(0,0,0,.35)">${br(c.head)}</h2>
   <div class="handle">${esc(c.handle)}</div></div>`,

"magazine": c=>`<img class="bg" src="${c.img}" alt=""><div class="abs" style="background:linear-gradient(rgba(0,0,0,.5),transparent 32%,transparent 52%,rgba(0,0,0,.72))"></div>
  <div class="abs" style="padding:5cqmin 6cqmin;color:#fff;display:flex;flex-direction:column">
   <div style="font:700 ${Math.min(17,92/Math.max(String(c.mark).length,3)).toFixed(1)}cqmin/.9 var(--f-serif);letter-spacing:-.02em;text-align:center;text-transform:uppercase">${esc(c.mark)}</div>
   <div style="display:flex;justify-content:space-between;gap:3cqmin;border-top:.2cqmin solid #fff;border-bottom:.2cqmin solid #fff;margin-top:1.5cqmin;padding:1cqmin 0;font:600 2.2cqmin/1.2 var(--f-sans);letter-spacing:.18em;text-transform:uppercase"><span>${esc(c.kicker)}</span><span>${esc(c.handle)}</span></div>
   <div style="margin-top:auto;max-width:72%">
    <div style="background:var(--ac);color:var(--on-ac);display:inline-block;padding:1cqmin 2cqmin;font:700 2.4cqmin/1.1 var(--f-sans);letter-spacing:.15em;text-transform:uppercase">Feature</div>
    <h2 style="margin-top:2.5cqmin;font:700 7.2cqmin/1.04 var(--f-serif)">${br(c.head)}</h2>
    <p style="margin-top:2cqmin;font:400 3cqmin/1.35 var(--f-sans)">${br(c.body)}</p></div></div>`,

"knockout": c=>{const w=words(c.head).sort((a,b)=>b.length-a.length)[0]||"";const fs=Math.min(c.land?40:34,(c.land?190:118)/Math.max(w.length,3));
 return `<div class="abs" style="background:var(--paper);color:var(--ink);padding:7cqmin;display:flex;flex-direction:column;justify-content:space-between">
  <div class="kick">${esc(c.kicker)}</div>
  <h2 style="font:400 ${fs.toFixed(1)}cqmin/.88 var(--f-disp);text-transform:uppercase;background:url('${c.img}') center/cover;-webkit-background-clip:text;background-clip:text;color:transparent;text-align:center;white-space:nowrap">${esc(w)}</h2>
  <div><div style="font:600 5.2cqmin/1.12 var(--f-serif)">${br(c.head)}</div><div class="handle" style="margin-top:2.5cqmin">${esc(c.handle)}</div></div></div>`},

"glass": c=>`<img class="bg" src="${c.img}" alt="" style="filter:saturate(1.1)"><div class="abs" style="background:rgba(0,0,0,.18)"></div>
  <div style="position:absolute;left:7cqmin;right:7cqmin;bottom:7cqmin;max-width:120cqmin;padding:6cqmin;border-radius:4cqmin;background:rgba(20,20,20,.38);backdrop-filter:blur(14px) saturate(1.3);-webkit-backdrop-filter:blur(14px) saturate(1.3);border:.25cqmin solid rgba(255,255,255,.35);color:#fff">
   <div class="kick">${esc(c.kicker)}</div>
   <h2 style="margin-top:2.5cqmin;font:600 7cqmin/1.08 var(--f-disp);letter-spacing:-.02em">${br(c.head)}</h2>
   <p style="margin-top:2.5cqmin;font:400 3.3cqmin/1.4 var(--f-sans)">${br(c.body)}</p></div>`,

"letterbox": c=>`<div class="abs" style="background:#000"></div><img src="${c.img}" alt="" style="position:absolute;left:0;right:0;top:18%;height:62%;width:100%;object-fit:cover">
  <div style="position:absolute;top:0;left:0;right:0;height:18%;display:flex;align-items:center;justify-content:space-between;gap:3cqmin;padding:0 6cqmin;color:#bbb;font:400 2.6cqmin/1.2 var(--f-mono)"><span>${esc(c.kicker)}</span><span>${esc(c.detail)}</span></div>
  <div style="position:absolute;left:0;right:0;top:80%;bottom:0;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:0 8cqmin">
   <h2 style="font:italic 500 5.4cqmin/1.2 var(--f-serif);color:#fff7d6">${br(c.head)}</h2>
   <div class="handle" style="color:#aaa;margin-top:1.6cqmin">${esc(c.handle)}</div></div>`,

"stickers": c=>`<img class="bg" src="${c.img}" alt="">
  <div style="position:absolute;left:6cqmin;top:8cqmin;max-width:80%;background:var(--ac2);color:var(--on-ac2);padding:2cqmin 3.5cqmin;transform:rotate(-5deg);font:400 4cqmin/1.1 var(--f-hand);box-shadow:.8cqmin .8cqmin 0 #111">${esc(c.kicker)}</div>
  <div style="position:absolute;left:6cqmin;right:10cqmin;bottom:17cqmin;background:#fff;color:#111;padding:4cqmin;transform:rotate(1.5deg);box-shadow:1.2cqmin 1.2cqmin 0 #111;border:.5cqmin solid #111">
   <h2 style="font:400 7.4cqmin/1.06 var(--f-disp);text-transform:uppercase;letter-spacing:-.02em">${br(c.head)}</h2></div>
  <div style="position:absolute;right:6cqmin;bottom:6cqmin;max-width:80%;background:var(--ac);color:var(--on-ac);padding:1.8cqmin 3cqmin;transform:rotate(-3deg);font:400 3.4cqmin/1.1 var(--f-hand);border:.4cqmin solid #111">${esc(c.handle)}</div>`,

"badge": c=>`<img class="bg" src="${c.img}" alt="">
  <div style="position:absolute;left:5cqmin;bottom:5cqmin;background:var(--bg);color:var(--fg);padding:2.6cqmin 3.6cqmin;border-radius:1cqmin;max-width:72%">
   <h2 style="font:600 3.8cqmin/1.18 var(--f-sans)">${br(c.head)}</h2>
   <div style="font:400 2.6cqmin/1.1 var(--f-sans);color:var(--mut-t);margin-top:1.4cqmin">${esc(c.handle)}</div></div>`,

"collage": c=>{const im=c.imgs.filter(x=>x!==c.img);const g=i=>im.length?im[i%im.length]:c.img;
 return `<div class="abs" style="background:var(--bg);padding:3cqmin;display:grid;grid-template-columns:1.3fr 1fr;grid-template-rows:1fr 1fr auto;gap:2cqmin">
  <img src="${c.img}" alt="" style="grid-row:1/3;width:100%;height:100%;object-fit:cover;border-radius:1cqmin;min-height:0">
  <img src="${g(0)}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:1cqmin;min-height:0">
  <img src="${g(1)}" alt="" style="width:100%;height:100%;object-fit:cover;border-radius:1cqmin;min-height:0">
  <div style="grid-column:1/3;padding:3cqmin 2cqmin 2cqmin;color:var(--fg);display:flex;justify-content:space-between;align-items:flex-end;gap:4cqmin">
   <div style="min-width:0"><div class="kick" style="color:var(--ac-t)">${esc(c.kicker)}</div><h2 style="margin-top:1.5cqmin;font:700 6cqmin/1.06 var(--f-disp);letter-spacing:-.02em">${br(c.head)}</h2></div>
   <div class="handle" style="white-space:nowrap">${esc(c.handle)}</div></div></div>`},

"arch": c=>`<div class="abs" style="background:var(--paper);color:var(--ink);display:flex;flex-direction:column;align-items:center;padding:7cqmin 10cqmin">
  <div class="kick" style="color:var(--ac-p);letter-spacing:.25em;text-align:center">${esc(c.kicker)}</div>
  <div style="margin-top:4cqmin;width:${c.land?"60cqmin":"100%"};flex:1;min-height:0;border-radius:50cqmin 50cqmin 0 0;overflow:hidden;border:.6cqmin solid var(--ac);outline:.2cqmin solid var(--ac);outline-offset:1.2cqmin"><img src="${c.img}" alt="" style="width:100%;height:100%;object-fit:cover;display:block"></div>
  <h2 style="margin-top:5cqmin;font:400 6.6cqmin/1.08 var(--f-serif);text-align:center">${br(c.head)}</h2>
  <div class="handle" style="margin-top:2.5cqmin;color:var(--mut-p)">${esc(c.handle)}</div></div>`,

"sideband": c=>`<img class="bg" src="${c.img}" alt="" style="left:${c.land?"38%":"30%"};width:${c.land?"62%":"70%"}">
  <div style="position:absolute;left:0;top:0;bottom:0;width:${c.land?"40%":"44%"};background:var(--bg);color:var(--fg);padding:7cqmin 5cqmin;display:flex;flex-direction:column">
   <div style="writing-mode:vertical-rl;transform:rotate(180deg);font:600 2.6cqmin/1 var(--f-sans);letter-spacing:.25em;text-transform:uppercase;color:var(--ac-t);max-height:45%;overflow:hidden">${esc(c.kicker)}</div>
   <h2 style="margin-top:auto;font:700 6.2cqmin/1.04 var(--f-disp);letter-spacing:-.02em;overflow-wrap:anywhere">${br(c.head)}</h2>
   <p style="margin-top:3cqmin;font:400 2.9cqmin/1.4 var(--f-sans);color:var(--mut-t)">${br(c.body)}</p>
   <div class="handle" style="margin-top:4cqmin;color:var(--ac-t);overflow-wrap:anywhere">${esc(c.handle)}</div></div>`,

"product": c=>`<div class="abs" style="background:var(--paper);color:var(--ink);display:grid;${c.land?"grid-template-columns:1fr 1fr":"grid-template-rows:60% 40%"}">
  <div style="position:relative;overflow:hidden;margin:4cqmin 4cqmin ${c.land?"4cqmin":"0"};border-radius:2cqmin"><img class="bg" src="${c.img}" alt=""></div>
  <div style="padding:5cqmin 6cqmin;display:flex;flex-direction:column;min-height:0">
   <div style="display:flex;justify-content:space-between;align-items:center;gap:3cqmin"><span class="kick" style="color:var(--ac-p)">${esc(c.kicker)}</span>${c.detail?`<span style="font:700 3cqmin/1 var(--f-sans);border:.3cqmin solid currentColor;border-radius:99cqmin;padding:1.4cqmin 2.6cqmin;white-space:nowrap">${esc(c.detail)}</span>`:""}</div>
   <h2 style="margin-top:2.5cqmin;font:900 9cqmin/.98 var(--f-disp);text-transform:uppercase;letter-spacing:-.01em">${br(c.head)}</h2>
   <p style="margin-top:2cqmin;font:400 3.2cqmin/1.4 var(--f-sans);color:var(--mut-p)">${br(c.body)}</p>
   <div class="handle" style="margin-top:auto;padding-top:2cqmin">${esc(c.handle)} →</div></div></div>`,

"quotephoto": c=>`<img class="bg" src="${c.img}" alt=""><div class="abs" style="background:linear-gradient(to top,rgba(0,0,0,.9),rgba(0,0,0,.55) 50%,rgba(0,0,0,.15))"></div>
  <div class="abs" style="padding:8cqmin;display:flex;flex-direction:column;justify-content:flex-end;color:#fff">
   <div style="font:400 26cqmin/.5 var(--f-serif);color:var(--ac-d);height:10cqmin" aria-hidden="true">“</div>
   <h2 style="font:italic 400 7.2cqmin/1.15 var(--f-serif)">${br(c.head)}</h2>
   <div style="margin-top:4cqmin;font:600 2.8cqmin/1.2 var(--f-sans);letter-spacing:.14em;text-transform:uppercase;color:var(--ac-d)">— ${esc(c.mark)} · ${esc(c.handle)}</div></div>`,

"beforeafter": c=>{const b=c.imgs.find(x=>x!==c.img)||c.img;
 return `<div class="abs" style="background:var(--bg);display:grid;grid-template-rows:auto 1fr auto;padding:4cqmin;gap:3cqmin;color:var(--fg)">
  <div class="kick" style="color:var(--ac-t);text-align:center">${esc(c.kicker)}</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:1cqmin;min-height:0">
   ${[[b,"Before",b===c.img?"filter:grayscale(1) brightness(.8)":""],[c.img,"After",""]].map(([src,l,f])=>`<div style="position:relative;overflow:hidden;border-radius:1cqmin"><img class="bg" src="${src}" alt="" style="${f}"><span style="position:absolute;left:2cqmin;top:2cqmin;background:var(--bg);color:var(--on-bg);font:700 2.6cqmin/1 var(--f-sans);padding:1.4cqmin 2.2cqmin;border-radius:99cqmin;text-transform:uppercase;letter-spacing:.1em">${l}</span></div>`).join("")}</div>
  <div style="text-align:center"><h2 style="font:700 6cqmin/1.08 var(--f-disp);letter-spacing:-.02em">${br(c.head)}</h2><div class="handle" style="margin-top:2cqmin;color:var(--mut-t)">${esc(c.handle)}</div></div></div>`}
};

import { z } from "zod";
export type HubPost = { id:string; kind:string; postedAt:number; source:string; title:string; summary:string; text:string; url:string; sentiment:string; tickers:string[]; mediaUrls:string[]; narrative:string; href?:string; stage?:string };
const parse = (v:unknown):any => { try { return JSON.parse(String(v ?? "{}")); } catch { return {}; } };
export function mergeHubPosts(posts:HubPost[], ticker:string) {
  const seen=new Set<string>();
  return posts.filter(p=>p.tickers.some(t=>t.toUpperCase()===ticker)&&p.postedAt>0).sort((a,b)=>b.postedAt-a.postedAt).filter(p=>{
    let key=`${p.kind}:${p.id}`;
    try{const url=new URL(p.url);url.hash="";for(const k of [...url.searchParams.keys()])if(/^(utm_|s$|t$|ref$)/.test(k))url.searchParams.delete(k);key=url.toString();}catch{/* Material pasted without a URL. */}
    if(seen.has(key))return false;seen.add(key);return true;
  }).slice(0,100);
}
export function hubConsensus(posts:HubPost[], now=Date.now()) {
  const daily=posts.filter(p=>p.postedAt>=now-86400000);
  const sources=new Map<string,HubPost>();
  for(const p of daily)if(p.postedAt>(sources.get(p.source)?.postedAt??0))sources.set(p.source,p);
  const opinions=[...sources.values()];
  const bullish=opinions.filter(p=>/bullish|long|positive/.test(p.sentiment)).length;
  const bearish=opinions.filter(p=>/bearish|short|negative/.test(p.sentiment)).length;
  return {posts24h:daily.length,sources24h:sources.size,bullish,bearish,neutral:opinions.length-bullish-bearish,
    label:!opinions.length?"Нет свежих данных":bullish&&bearish?"Мнения расходятся":bullish?"Преобладает позитив":bearish?"Преобладает негатив":"Без направления"};
}
export const digestSchema=z.object({headline:z.string().max(600),narrative_shift:z.string().max(2500),for_portfolio:z.string().max(1500),confidence:z.enum(["low","medium","high"]),facts:z.array(z.object({text:z.string().max(1200),sourceIds:z.array(z.string()).min(1).max(6)})).max(8),risks:z.array(z.string().max(600)).max(6),watch:z.array(z.string().max(600)).max(6)});

import { z } from 'zod';

export const candidateSchema = z.object({
  ticker:z.string().regex(/^[A-Z0-9]{1,12}$/), project:z.string().min(2).max(100),
  event_key:z.string().regex(/^[a-z0-9-]{3,100}$/),
  kind:z.enum(['architecture','launch','tokenomics','listing','partnership','funding','governance','security','regulatory']),
  stage:z.enum(['proposal','announced','live','rumor','denied']),
  title:z.string().min(5).max(180), summary:z.string().min(10).max(1200),
  why:z.string().min(10).max(1000), checks:z.array(z.string().min(5).max(240)).min(1).max(4),
  quote:z.string().min(12).max(350), confidence:z.number().min(0).max(1), impact:z.number().int().min(1).max(5),
});
export type Candidate=z.infer<typeof candidateSchema>;
export type SignalPost={id:string;kind:'x'|'telegram'|'infopole';text:string;url:string;author:string;postedAt:number;apiVerified?:boolean};
export type Signal={id:string;detectedAt:number;seen:boolean;candidate:Candidate;model:string;evidence:SignalPost[];baseline:{price:number;at:number;provider:string}|null;current:{price:number;at:number;provider:string}|null;change:number|null};
export const stageLabels={proposal:'Обсуждение / предложение',announced:'Объявлено',live:'Запущено',rumor:'Неподтверждённый слух',denied:'Опровержение'};
export const officialAssets=[{ticker:'STRK',symbol:'STRK',name:'Starknet',handle:'Starknet',coingeckoId:'starknet',binanceSymbol:'STRKUSDT'}];
export function isPotentialUpdate(text:string){return /\b(l1|l2|mainnet|testnet|upgrad\w*|migrat\w*|quantum|launch\w*|announc\w*|propos\w*|roadmap|tokenomics|buyback|partnership\w*|funding|exploit\w*|approv\w*|list(?:ing|ed)|updat\w*|airdrop|burn(?:ed|ing)?)\b|обновлен|обновлён|запуск|переход|миграц|квантов|рассматрива|партн[её]р|выкуп|сжиган|инвестиц|предложен|одобрен|взлом|листинг/i.test(text);}
const normalized=(s:string)=>s.replace(/\s+/g,' ').trim().toLowerCase();
export function supportedCandidate(c:Candidate,p:SignalPost,assets:{ticker:string;name:string}[]){
  if(!normalized(p.text).includes(normalized(c.quote)))return false;
  if(c.stage==='live'&&/actively considering|рассматрива|предлагаем|proposing/i.test(p.text)&&! /\blaunched\b|\bwent live\b|запущен/i.test(p.text))return false;
  const asset=assets.find(a=>a.ticker===c.ticker);
  const symbol=new RegExp(`(?:^|[^A-Za-z0-9])[$#]?${c.ticker}(?:$|[^A-Za-z0-9])`);
  return symbol.test(p.text)||!!(asset&&asset.name.length>=4&&normalized(p.text).includes(normalized(asset.name)));
}
export function canonicalEventKey(c:Candidate){
  const key=c.kind==='architecture'&&(/l1|layer-1/.test(c.event_key))?'l1-migration':c.event_key;
  return `${c.ticker}:${c.kind}:${key}:${c.stage}`;
}
export function shouldAlert(c:Candidate,postedAt:number,now=Date.now()){
  return c.confidence>=.75&&c.impact>=4&&c.stage!=='rumor'&&postedAt<=now+60000&&now-postedAt<=48*3600000;
}
export function priceReaction(baseline:Signal['baseline'],current:Signal['current']){
  if(!baseline||!current||baseline.price<=0||current.price<=0||current.at<baseline.at||baseline.provider!==current.provider)return null;
  return (current.price/baseline.price-1)*100;
}

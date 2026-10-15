/** Adapters only. Algorithms live in the imported STVTerminal modules. */
import { mergeHubPosts, hubConsensus, digestSchema } from "./services/ticker_recap/hub-core";
import { isAssetMention } from "./services/ticker_recap/mention-identity";
import { candidateSchema, supportedCandidate, canonicalEventKey } from "./services/project-signals/model";
import { classify } from "./services/market/assetResolution";
import { matches } from "./src/events/merge";
import { scoreImportance } from "./src/events/importance";
import { buildStories } from "./src/telegram/stories";
import { meaningfulText } from "./src/telegram/noise";
import { webReader,parseTmePage } from "./src/live/tmeReader";
import * as readline from "node:readline";
import { indexValue } from "./services/baskets/index-model";
import { deriveIdeaMetrics, evaluateCancelRule } from "./services/investment-ideas/analytics";
import { createIdeaSchema } from "./services/investment-ideas/validation";
import { rankBySimilarity } from "./src/playbook/similar";
import { checkCondition, mergeConditions } from "./src/playbook/conditions";
import { evaluateGroup } from "./services/backtest/rules/registry";
import { MATCH_PROMPT, MATCH_SCHEMA } from "./src/playbook/match";
import { DISTILL_PROMPT, RULE_SCHEMA, validateRule } from "./src/playbook/distill";
import { strategyConfigSchema } from "./services/backtest/validation";

function nativeEvent(value: any): any {
  return { ...value, date: value.date_label?.slice(0,10) || null,
    precision: value.precision || "unknown", tickers: value.asset_uids || [],
    projects: [], status: value.review_status === "accepted" ? "verified" : "review",
    sources: (value.sources || []).map((s:any)=>({...s,kind:s.namespace||s.kind,ref:s.external_id||s.id})),
    startsAt:value.starts_at ?? null, valueUsd:value.amount_unit==="USD"?value.amount:null,
    title: value.title || "", kind: value.kind || "other" };
}
export function execute(input: any): any {
  if(input.action==="external_idea") return createIdeaSchema.parse(input.value);
  if(input.action==="rule_validate") return validateRule(input.value,input.examples||[]);
  if(input.action==="strategy_validate") return strategyConfigSchema.parse(input.config);
  if(input.action==="rule_signals") {
    const result:any[]=[];
    for(const [id,raw] of Object.entries(input.series)) {
      const bars=raw as any[];
      for(let i=0;i<bars.length;i++) {
        const now=bars[i].close_boundary;
        let first=Math.max(0,i-365);
        for(let j=i;j>first;j--)if(!bars[j-1].valid||bars[j].open_time-bars[j-1].open_time!==86400000){first=j;break;}
        const history=bars.slice(first,i+1).map((b:any)=>({...b,ts:b.open_time}));
        let benchmarkHistory=input.benchmark.filter((b:any)=>b.close_boundary<=now).slice(-366).map((b:any)=>({...b,ts:b.open_time}));
        for(let j=benchmarkHistory.length-1;j>0;j--)if(!benchmarkHistory[j-1].valid||benchmarkHistory[j].open_time-benchmarkHistory[j-1].open_time!==86400000){benchmarkHistory=benchmarkHistory.slice(j);break;}
        if(benchmarkHistory.at(-1)?.close_boundary!==now)benchmarkHistory=[];
        if(bars[i].valid) result.push({instrument_id:id,signal_time:now,entry:evaluateGroup(input.entry,{history,benchmarkHistory}),
          exit:evaluateGroup(input.exit||{op:'OR',rules:[]},{history,benchmarkHistory})});
      }
    }
    return result;
  }
  if(input.action==="playbook_prompt") return input.operation==="distill"?{prompt:DISTILL_PROMPT,schema:RULE_SCHEMA}:{prompt:MATCH_PROMPT,schema:MATCH_SCHEMA};
  if(input.action==="basket_indexes") return input.baskets.map((assets:any)=>indexValue(assets));
  if(input.action==="idea_metrics") return deriveIdeaMetrics(input.snapshots,input.price,input.now,input.started_at);
  if(input.action==="idea_cancel") return evaluateCancelRule(input.rule,input.context);
  if(input.action==="lexical") return rankBySimilarity(input.query,input.items,input.extra_docs||[]);
  if(input.action==="conditions") return {results:input.conditions.map((c:any)=>checkCondition(c,input.source)),
    merged:mergeConditions(input.current||[],input.proposed||[])};
  if(input.action==="rules") return input.contexts.map((context:any)=>evaluateGroup(input.group,context));
  if(input.action==="parse_telegram") return parseTmePage(input.html,input.handle);
  if(input.action==="collect_telegram") return webReader({
    fetch:(url:any,options:any)=>fetch(url,{...options,signal:AbortSignal.timeout(12000)}),
    maxPages:3
  }).fetchSince(input.handle,input.since??null,{backfill:15,maxPosts:30});
  if (input.action === "hub") {
    const posts = mergeHubPosts(input.posts, input.ticker);
    return { posts, consensus: hubConsensus(posts, input.now) };
  }
  if (input.action === "summary") return digestSchema.parse(input.value);
  if (input.action === "identity") return {
    asset_class: classify(input.mention),
    is_asset_mention: isAssetMention(input.mention.symbol, input.text || "", input.author || ""),
  };
  if (input.action === "project") {
    const candidate = candidateSchema.parse(input.candidate);
    return { candidate, supported: supportedCandidate(candidate, input.post, input.assets || []),
      canonical_event_key: canonicalEventKey(candidate) };
  }
  if (input.action === "calendar") {
    const candidate = nativeEvent(input.candidate);
    return { importance: scoreImportance(candidate, new Set(input.watched || []),input.now),
      matches: (input.existing || []).filter((item: any) => matches(nativeEvent(item), candidate)).map((item: any) => item.id) };
  }
  if (input.action === "stories") return buildStories(input.posts);
  if (input.action === "meaningful_text") return meaningfulText(input.text || "");
  throw new Error("unsupported_action");
}
if (require.main === module) {
  const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
  lines.on("line", line => {
    Promise.resolve().then(()=>execute(JSON.parse(line)))
      .then(data=>process.stdout.write(JSON.stringify({ok:true,data})+"\n"))
      .catch(()=>process.stdout.write(JSON.stringify({ok:false,error:"terminal_validation"})+"\n"));
  });
}

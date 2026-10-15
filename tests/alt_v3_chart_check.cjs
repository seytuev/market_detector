const assert = require('node:assert/strict');
const chart = require('../app/web/static/alt_chart.js');
const day = 86400000;
const ts = s => Date.parse(s+'T00:00:00Z');
const detail = {
  kind:'structure', as_of_ms:ts('2026-10-07'),
  frozen_range:{lower:1.55,upper:3.383,mid:2.4665},
  effective_range:{lower:1.55,upper:3.383,mid:2.4665,geometry_source:'structural',rules_version:'alt-0.5',
    range_id:5,revision_id:'closed',base_start_open_time:ts('2025-04-07'),base_end_open_time:ts('2026-09-20')},
  pd_zones:[
    {id:'fvg',kind:'FVG',lower:1.692,upper:2.215,available_at_ms:ts('2026-06-01'),invalidated_at_ms:ts('2026-08-10')},
    {id:'ob',kind:'OB',lower:1.469,upper:1.692,available_at_ms:ts('2026-05-25'),invalidated_at_ms:null},
  ],
  cancel:{price:.841},scenarios:[{bos_level:1.894}],
  candles:[],manipulation_episodes:[],
};
const view={range:true,entries:true,manipulation:true,eventMode:'setup',cancel:true};
const boxes=chart.collectBoxes(detail,view,ts('2026-10-06')).boxes;
const base=boxes.find(b=>b.kind==='range');
assert.equal(base.lower,1.55);
assert.equal(base.endMs,ts('2026-09-21'));
const fvg=boxes.find(b=>b.kind==='pd_fvg');
assert.deepEqual([fvg.lower,fvg.upper,fvg.startMs,fvg.endMs],[1.692,2.215,ts('2026-06-01'),ts('2026-08-10')]);
assert.equal(boxes.find(b=>b.kind==='pd_ob').endMs,ts('2026-10-07'));
assert(!boxes.some(b=>b.kind==='retest')); // no legacy [M,U] entry rectangle
const levels=chart.collectLevels(detail,view,5).levels;
assert(levels.some(l=>l.name==='BOS W1'&&l.price===1.894));
assert(levels.some(l=>l.name==='K'&&l.price===.841));
console.log('W1 chart geometry, PD lifetimes, BOS and cancellation: OK');

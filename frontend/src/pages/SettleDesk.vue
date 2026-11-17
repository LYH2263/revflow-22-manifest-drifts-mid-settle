<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const runs = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)
const detailId = ref(null)
const detail = ref(null)
const detailErr = ref('')

async function load() {
  runs.value = await getJson('/api/settle-runs')
  if (detailId.value) await openManifest(detailId.value)
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runSettle() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs')
    msg.value = `归并完成 #${r.id}：清单 ${r.manifest_count} 笔，净额 ${yuan(r.net_cents)} 元，隔离 ${r.quarantine_count} 笔，清单外新到达 ${r.outside_arrival_count} 笔`
    await load()
    await openManifest(r.id)
  } catch (e) {
    err.value = String(e.message || e)
    await load().catch(() => {})
  } finally {
    busy.value = false
  }
}

async function freezeOnly() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs?freeze_only=true')
    msg.value = `已固化 #${r.id} 本趟消费清单（${r.manifest_count} 笔），待配平`
    await load()
    await openManifest(r.id)
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function balanceRun(run) {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson(`/api/settle-runs/${run.id}/balance`)
    msg.value = `清单 #${r.id} 配平完成：成功 ${r.settled_count} / 清单 ${r.manifest_count}，清单外新到达 ${r.outside_arrival_count} 笔`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
    await load().catch(() => {})
  } finally {
    busy.value = false
  }
}

async function openManifest(runId) {
  detailId.value = runId
  detailErr.value = ''
  try {
    detail.value = await getJson(`/api/settle-runs/${runId}/manifest`)
  } catch (e) {
    detail.value = null
    detailErr.value = String(e.message || e)
  }
}

function closeManifest() {
  detailId.value = null
  detail.value = null
  detailErr.value = ''
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

const reconciled = computed(() => {
  if (!detail.value) return false
  const s = detail.value.summary
  return s.清单成员数 === s.成功数 + s.隔离数 + s.未执行数 && s.未执行数 === 0
})

const proof = computed(() => detail.value?.proof || null)
</script>

<template>
  <div>
    <h1>归并台</h1>
    <p class="lead">
      开跑瞬间固化本趟候选冲正键集合，本趟只处理固化清单内成员；清单外新到达流水留待后续趟，不得进本趟正式配平。
    </p>
    <p class="note-strip">
      清单持久在库（独立清单表挂归并运行 id + 不可变成员行）；配平查询只读清单快照，提交前再校验实际触及键 ⊆ 固化清单，越界整趟回滚；失败则清单标记失败、正式余额不变。每趟另出一份清单对照证明（固化键/实际配平键/漂移键/隔离键）。
    </p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runSettle">跑净额归并</button>
      <button class="btn secondary" :disabled="busy" @click="freezeOnly">仅固化本趟清单</button>
    </div>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>

    <div class="panel">
      <h2>归并运行</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>状态</th><th>清单成员</th><th>正向</th><th>冲正</th>
            <th>成功</th><th>隔离</th><th>清单外到达</th><th>净额（元）</th><th>结束</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td>{{ r.id }}</td>
            <td>
              <span class="tag" :class="{ on: r.status === '完成', warn: r.status === '清单已固化', bad: r.status === '失败' }">
                {{ r.status }}
              </span>
            </td>
            <td>{{ r.manifest_count }}</td>
            <td>{{ r.forward_count }}</td>
            <td>{{ r.reversal_count }}</td>
            <td>{{ r.status === '完成' ? r.settled_count : '—' }}</td>
            <td>{{ r.status === '完成' ? r.quarantine_count : '—' }}</td>
            <td>{{ r.status === '完成' ? r.outside_arrival_count : '—' }}</td>
            <td>{{ r.status === '完成' ? yuan(r.net_cents) : '—' }}</td>
            <td>{{ r.finished_at || '—' }}</td>
            <td>
              <button v-if="r.status === '清单已固化'" class="btn secondary" :disabled="busy" @click="balanceRun(r)">
                配平此清单
              </button>
              <button v-else class="btn secondary" @click="openManifest(r.id)">查看清单</button>
            </td>
          </tr>
          <tr v-if="!runs.length"><td colspan="11">尚无归并运行</td></tr>
        </tbody>
      </table>
      <p v-for="r in runs.filter((x) => x.status === '失败')" :key="'f' + r.id" class="msg">
        #{{ r.id }} 失败：{{ r.failure_reason }}
      </p>
    </div>

    <div v-if="detailId" class="panel">
      <h2>
        本趟消费清单 · 归并 #{{ detailId }}
        <button class="btn secondary" @click="closeManifest">收起</button>
      </h2>
      <p v-if="detailErr" class="msg">{{ detailErr }}</p>
      <template v-if="detail">
        <div class="stats">
          <div class="stat">
            <div class="k">清单成员</div>
            <div class="v">{{ detail.summary.清单成员数 }}</div>
          </div>
          <div class="stat">
            <div class="k">成功 / 隔离 / 未执行</div>
            <div class="v">
              {{ detail.summary.成功数 }} / {{ detail.summary.隔离数 }} / {{ detail.summary.未执行数 }}
            </div>
          </div>
          <div class="stat">
            <div class="k">清单外新到达</div>
            <div class="v">{{ detail.summary.清单外新到达笔数 }}</div>
          </div>
        </div>
        <p class="note-strip" :class="{ msg: !reconciled }">
          对账：清单 {{ detail.summary.清单成员数 }} = 成功 {{ detail.summary.成功数 }} +
          隔离 {{ detail.summary.隔离数 }} + 未执行 {{ detail.summary.未执行数 }}；清单外新到达
          {{ detail.summary.清单外新到达笔数 }} 笔留待后续趟。
          <template v-if="detail.manifest.status === '已固化'">清单已固化，尚未配平。</template>
          <template v-else-if="detail.manifest.status === '失败'">清单失败：{{ detail.manifest.failure_reason }}</template>
        </p>

        <table>
          <thead>
            <tr><th>流水键</th><th>方向</th><th>账户</th><th>金额（元）</th><th>原单键</th><th>结果</th><th>实时状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="m in detail.members" :key="m.id">
              <td>{{ m.entry_key }}</td>
              <td>{{ m.kind }}</td>
              <td>{{ m.account_id }}</td>
              <td>{{ yuan(m.amount_cents) }}</td>
              <td>{{ m.target_entry_key || '—' }}</td>
              <td>
                <span class="tag" :class="{ on: m.outcome === '已归并', warn: m.outcome === '已隔离', bad: m.outcome === '未执行' }">
                  {{ m.outcome }}
                </span>
                <em v-if="m.result_reason" class="reason">{{ m.result_reason }}</em>
              </td>
              <td>{{ m.live_status }}</td>
            </tr>
          </tbody>
        </table>

        <h2 style="margin-top: 22px">清单外新到达（留待后续趟）</h2>
        <table>
          <thead>
            <tr><th>流水键</th><th>方向</th><th>金额（元）</th><th>入账时间</th><th>检出</th></tr>
          </thead>
          <tbody>
            <tr v-for="x in detail.exclusions" :key="x.id">
              <td>{{ x.entry_key }}</td>
              <td>{{ x.kind }}</td>
              <td>{{ yuan(x.amount_cents) }}</td>
              <td>{{ x.booked_at }}</td>
              <td>{{ x.detected_at }}</td>
            </tr>
            <tr v-if="!detail.exclusions.length"><td colspan="5">本趟无清单外新到达</td></tr>
          </tbody>
        </table>

        <h2 style="margin-top: 22px">
          清单对照证明 · 归并 #{{ detailId }}
          <span v-if="proof" class="tag" :class="{ on: proof.status === '已配平', bad: proof.status === '失败' }">
            {{ proof.status }}
          </span>
        </h2>
        <p v-if="!proof" class="note-strip">清单已固化尚未配平，配平或失败闭后在此生成对照证明（与清单、流水、隔离同库同源）。</p>
        <template v-else>
          <div class="stats">
            <div class="stat">
              <div class="k">固化键</div>
              <div class="v">{{ proof.frozen_count }}</div>
            </div>
            <div class="stat">
              <div class="k">实际配平键</div>
              <div class="v">{{ proof.settled_count }}</div>
            </div>
            <div class="stat">
              <div class="k">漂移键（清单外）</div>
              <div class="v">{{ proof.drift_count }}</div>
            </div>
            <div class="stat">
              <div class="k">隔离键</div>
              <div class="v">{{ proof.quarantine_count }}</div>
            </div>
            <div class="stat">
              <div class="k">收口结论</div>
              <div class="v">
                <span class="tag" :class="proof.containment_ok ? 'on' : 'bad'">
                  {{ proof.containment_ok ? '实际触及键 ⊆ 固化清单' : '越界失败' }}
                </span>
              </div>
            </div>
          </div>
          <p v-if="proof.failure_reason" class="msg">失败原因：{{ proof.failure_reason }}</p>
          <p class="note-strip" :class="{ msg: !proof.containment_ok }">
            配平/隔离键全部来自固化键集合，漂移键不计入本趟正式余额；证明一经写入不可改、不可删，
            可经只读接口 <code>GET /api/settle-runs/{{ detailId }}/proof</code> 按归并运行 id 调取。
          </p>
          <table>
            <thead>
              <tr><th>对照项</th><th>笔数</th><th>流水键清单</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>固化键（开跑瞬间钉扎）</td>
                <td>{{ proof.frozen_count }}</td>
                <td class="keys">{{ proof.frozen_keys.join('、') || '—' }}</td>
              </tr>
              <tr>
                <td>实际配平键（进正式余额）</td>
                <td>{{ proof.settled_count }}</td>
                <td class="keys">{{ proof.settled_keys.join('、') || '—' }}</td>
              </tr>
              <tr>
                <td>漂移键（清单外，留待后续趟）</td>
                <td>{{ proof.drift_count }}</td>
                <td class="keys">{{ proof.drift_keys.join('、') || '—' }}</td>
              </tr>
              <tr>
                <td>隔离键（未平/超额）</td>
                <td>{{ proof.quarantine_count }}</td>
                <td class="keys">{{ proof.quarantine_keys.join('、') || '—' }}</td>
              </tr>
            </tbody>
          </table>
        </template>
      </template>
    </div>
  </div>
</template>

<style scoped>
.panel h2 {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.panel h2 .btn {
  padding: 6px 12px;
  font-size: 12px;
}
.reason {
  display: block;
  margin-top: 3px;
  font-style: normal;
  font-size: 11px;
  color: var(--muted);
}
.keys {
  font-family: var(--mono, monospace);
  font-size: 12px;
  word-break: break-all;
  max-width: 640px;
}
</style>

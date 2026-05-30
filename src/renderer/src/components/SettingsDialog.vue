<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useBackend } from '../composables/useBackend'

type SettingSection = 'main' | 'advanced' | 'model'

interface SettingSchema {
  key: string
  label: string
  type: 'int' | 'float' | 'str_list' | 'str_set' | 'string'
  default: number | string | string[]
  section: SettingSection
  group: string
  help?: string
  min?: number
  max?: number
  step?: number
  choices?: string[]
}

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const backend = useBackend()
const schema = ref<SettingSchema[]>([])
const values = ref<Record<string, any>>({})
const defaults = ref<Record<string, any>>({})
const activeTab = ref<SettingSection>('main')
const loading = ref(false)
const saving = ref(false)
const error = ref<string | null>(null)
const dirty = ref(false)

async function loadSettings(): Promise<void> {
  loading.value = true
  error.value = null
  try {
    const resp = await backend.send<{
      schema: SettingSchema[]
      values: Record<string, any>
      defaults: Record<string, any>
    }>('settings.get')
    if (!resp.ok) throw new Error(resp.error?.message ?? 'settings.get failed')
    schema.value = resp.payload!.schema
    values.value = { ...resp.payload!.values }
    defaults.value = { ...resp.payload!.defaults }
    dirty.value = false
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  saving.value = true
  error.value = null
  try {
    const resp = await backend.send<{ values: Record<string, any> }>('settings.update', {
      values: values.value,
    })
    if (!resp.ok) throw new Error(resp.error?.message ?? 'settings.update failed')
    values.value = { ...resp.payload!.values }
    dirty.value = false
    emit('close')
  } catch (err) {
    error.value = (err as Error).message
  } finally {
    saving.value = false
  }
}

function resetToDefaults(): void {
  if (!confirm('確定要把所有參數還原成預設值？')) return
  values.value = { ...defaults.value }
  dirty.value = true
}

function onFieldChange(): void {
  dirty.value = true
}

function toggleChoice(key: string, choice: string): void {
  const arr = values.value[key] as string[]
  const idx = arr.indexOf(choice)
  if (idx >= 0) arr.splice(idx, 1)
  else arr.push(choice)
  dirty.value = true
}

function close(): void {
  if (dirty.value && !confirm('有未儲存的變更，確定關閉？')) return
  emit('close')
}

watch(() => props.open, (open) => {
  if (open) loadSettings()
})

// 依 section + group 分組
const groupedMain = computed(() => groupBy(schema.value.filter(s => s.section === 'main')))
const groupedAdvanced = computed(() => groupBy(schema.value.filter(s => s.section === 'advanced')))

// 本地/雲端 toggle
const LOCAL_ONLY_KEYS = new Set([
  'correction.model_repo', 'correction.model_file',
  'correction.n_ctx', 'correction.n_gpu_layers', 'correction.parallel_workers',
])
const CLOUD_ONLY_KEYS = new Set([
  'correction.api_base_url', 'correction.api_key', 'correction.api_model',
])
const HIDDEN_KEYS = new Set(['correction.backend'])

const correctionMode = computed<'local' | 'api'>(() =>
  (values.value['correction.backend'] === 'api') ? 'api' : 'local'
)

function setCorrectionMode(mode: 'local' | 'api') {
  values.value['correction.backend'] = mode
  dirty.value = true
}

const groupedModel = computed(() => {
  const mode = correctionMode.value
  const filtered = schema.value.filter(s => {
    if (s.section !== 'model') return false
    if (HIDDEN_KEYS.has(s.key)) return false
    if (mode === 'local' && CLOUD_ONLY_KEYS.has(s.key)) return false
    if (mode === 'api' && LOCAL_ONLY_KEYS.has(s.key)) return false
    return true
  })
  return groupBy(filtered)
})

const currentGrouped = computed(() => {
  if (activeTab.value === 'main') return groupedMain.value
  if (activeTab.value === 'advanced') return groupedAdvanced.value
  return groupedModel.value
})

function groupBy(items: SettingSchema[]): Record<string, SettingSchema[]> {
  const out: Record<string, SettingSchema[]> = {}
  for (const it of items) {
    if (!out[it.group]) out[it.group] = []
    out[it.group].push(it)
  }
  return out
}
</script>

<template>
  <div v-if="open" class="settings-overlay" @click.self="close">
    <div class="settings-dialog">
      <header class="settings-header">
        <h2>⚙️ 進階設定</h2>
        <button class="settings-close" @click="close" aria-label="關閉">✕</button>
      </header>

      <div class="settings-tabs">
        <button
          :class="['tab', { active: activeTab === 'main' }]"
          @click="activeTab = 'main'"
        >主要</button>
        <button
          :class="['tab', { active: activeTab === 'advanced' }]"
          @click="activeTab = 'advanced'"
        >進階</button>
        <button
          :class="['tab', { active: activeTab === 'model' }]"
          @click="activeTab = 'model'"
        >✨ 模型</button>
      </div>

      <div class="settings-body">
        <div v-if="loading" class="settings-loading">載入中…</div>
        <div v-else-if="error" class="settings-error">{{ error }}</div>

        <template v-else>
          <!-- 模型 tab：本地 / 雲端 toggle -->
          <div v-if="activeTab === 'model'" class="backend-toggle">
            <span class="toggle-label">校正後端</span>
            <div class="toggle-group">
              <button
                :class="['toggle-btn', { active: correctionMode === 'local' }]"
                @click="setCorrectionMode('local')"
              >🖥 本地</button>
              <button
                :class="['toggle-btn', { active: correctionMode === 'api' }]"
                @click="setCorrectionMode('api')"
              >☁️ 雲端 API</button>
            </div>
          </div>

          <div
            v-for="(items, groupName) in currentGrouped"
            :key="groupName"
            class="settings-group"
          >
            <h3 class="group-title">{{ groupName }}</h3>

            <div v-for="item in items" :key="item.key" class="settings-field">
              <label class="field-label">
                <span class="field-name">{{ item.label }}</span>
                <span v-if="item.help" class="field-help" :title="item.help">ⓘ</span>
              </label>

              <!-- int / float input -->
              <div v-if="item.type === 'int' || item.type === 'float'" class="field-input">
                <input
                  type="number"
                  :value="values[item.key]"
                  :min="item.min"
                  :max="item.max"
                  :step="item.step ?? (item.type === 'int' ? 1 : 0.01)"
                  @input="(e) => {
                    const v = (e.target as HTMLInputElement).value
                    values[item.key] = item.type === 'int' ? parseInt(v) : parseFloat(v)
                    onFieldChange()
                  }"
                />
                <span class="field-default">預設 {{ item.default }}</span>
              </div>

              <!-- str_set: 多選 checkbox -->
              <div v-else-if="item.type === 'str_set' && item.choices" class="field-checkboxes">
                <label v-for="c in item.choices" :key="c" class="checkbox-item">
                  <input
                    type="checkbox"
                    :checked="(values[item.key] as string[]).includes(c)"
                    @change="toggleChoice(item.key, c)"
                  />
                  {{ c }}
                </label>
              </div>

              <!-- str_list 有 choices → 單選 dropdown（用 list[0] 存單字串） -->
              <div v-else-if="item.type === 'str_list' && item.choices" class="field-input">
                <select
                  :value="(values[item.key] as string[])[0] ?? ''"
                  @change="(e) => {
                    values[item.key] = [(e.target as HTMLSelectElement).value]
                    onFieldChange()
                  }"
                >
                  <option v-for="c in item.choices" :key="c" :value="c">{{ c }}</option>
                </select>
                <span class="field-default">預設 {{ (item.default as string[])[0] }}</span>
              </div>

              <!-- string → 純文字輸入（API key、URL 等） -->
              <div v-else-if="item.type === 'string'" class="field-input">
                <input
                  type="text"
                  :value="values[item.key] ?? ''"
                  @input="(e) => {
                    values[item.key] = (e.target as HTMLInputElement).value
                    onFieldChange()
                  }"
                  style="width: 280px;"
                />
                <span v-if="item.default !== ''" class="field-default">預設 {{ item.default }}</span>
              </div>

              <!-- str_list 沒 choices → 文字輸入 -->
              <div v-else-if="item.type === 'str_list'" class="field-input">
                <input
                  type="text"
                  :value="(values[item.key] as string[])[0] ?? ''"
                  @input="(e) => {
                    values[item.key] = [(e.target as HTMLInputElement).value]
                    onFieldChange()
                  }"
                  style="width: 280px;"
                />
                <span class="field-default">預設 {{ (item.default as string[])[0] }}</span>
              </div>

              <p v-if="item.help" class="field-hint">{{ item.help }}</p>
            </div>
          </div>
        </template>
      </div>

      <footer class="settings-footer">
        <button class="reset" @click="resetToDefaults" :disabled="loading || saving">
          ↺ 還原預設
        </button>
        <div class="spacer" />
        <button class="cancel" @click="close" :disabled="saving">取消</button>
        <button
          class="save"
          @click="save"
          :disabled="loading || saving || !dirty"
        >
          {{ saving ? '儲存中…' : (dirty ? '儲存（下次錄音生效）' : '已儲存') }}
        </button>
      </footer>
    </div>
  </div>
</template>

<style scoped>
.settings-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.35);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
  backdrop-filter: blur(2px);
}

.settings-dialog {
  width: min(720px, 92vw);
  max-height: 88vh;
  background: #f8fafb;
  color: #182026;
  border-radius: 12px;
  border: 1px solid #d7dee4;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.12);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.settings-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid #d7dee4;
  background: #ffffff;
}

.settings-header h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
}

.settings-close {
  background: none;
  border: none;
  color: #64717d;
  font-size: 18px;
  cursor: pointer;
  padding: 4px 10px;
  border-radius: 6px;
  height: auto;
}

.settings-close:hover { background: #eef1f4; }

.settings-tabs {
  display: flex;
  border-bottom: 1px solid #d7dee4;
  padding: 0 20px;
  background: #ffffff;
}

.tab {
  background: none;
  border: none;
  color: #64717d;
  padding: 10px 20px;
  cursor: pointer;
  font-size: 14px;
  border-bottom: 2px solid transparent;
  height: auto;
}

.tab.active {
  color: #182026;
  border-bottom-color: #4f7ef8;
  font-weight: 600;
}

.tab:hover:not(.active) { color: #182026; background: #eef1f4; }

.settings-body {
  flex: 1;
  overflow-y: auto;
  padding: 18px 22px;
}

.settings-loading,
.settings-error {
  text-align: center;
  padding: 32px;
  color: #64717d;
}

.settings-error { color: #d94f4f; }

.settings-group {
  margin-bottom: 22px;
}

.group-title {
  margin: 0 0 12px 0;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  color: #64717d;
}

.settings-field {
  margin-bottom: 14px;
  padding-bottom: 14px;
  border-bottom: 1px solid #eef1f4;
}

.settings-field:last-child {
  border-bottom: none;
}

.field-label {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.field-name {
  font-size: 14px;
  font-weight: 500;
}

.field-help {
  cursor: help;
  color: #64717d;
  font-size: 12px;
}

.field-input {
  display: flex;
  align-items: center;
  gap: 10px;
}

.field-input input,
.field-input select {
  width: 120px;
  padding: 6px 10px;
  border: 1px solid #c8d1d9;
  background: #ffffff;
  color: #182026;
  border-radius: 6px;
  font-size: 14px;
  font-family: monospace;
  height: auto;
}

.field-input input[style*="280px"],
.field-input select {
  width: 280px;
}

.field-input input:focus,
.field-input select:focus {
  outline: none;
  border-color: #4f7ef8;
  box-shadow: 0 0 0 3px rgba(79, 126, 248, 0.12);
}

.field-default {
  font-size: 11px;
  color: #64717d;
  font-family: monospace;
}

.field-checkboxes {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
}

.checkbox-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border: 1px solid #c8d1d9;
  border-radius: 6px;
  font-size: 13px;
  font-family: monospace;
  cursor: pointer;
  background: #ffffff;
}

.checkbox-item:hover { background: #eef1f4; }

.checkbox-item input {
  margin: 0;
}

.field-hint {
  margin: 6px 0 0 0;
  font-size: 12px;
  color: #64717d;
  line-height: 1.5;
}

.settings-footer {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px 20px;
  border-top: 1px solid #d7dee4;
  background: #ffffff;
}

.spacer { flex: 1; }

.settings-footer button {
  padding: 8px 16px;
  border-radius: 6px;
  border: 1px solid #c8d1d9;
  background: #ffffff;
  color: #182026;
  cursor: pointer;
  font-size: 13px;
  height: auto;
}

.settings-footer button:hover:not(:disabled) { background: #eef1f4; }

.settings-footer button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.settings-footer button.save {
  background: #4f7ef8;
  border-color: #4f7ef8;
  color: #ffffff;
  font-weight: 600;
}

.settings-footer button.save:not(:disabled):hover {
  background: #3d6ee8;
  border-color: #3d6ee8;
}

.settings-footer button.reset {
  color: #64717d;
}

/* 本地 / 雲端 toggle */
.backend-toggle {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
  padding-bottom: 16px;
  border-bottom: 1px solid #d7dee4;
}

.toggle-label {
  font-size: 14px;
  font-weight: 500;
  color: #182026;
}

.toggle-group {
  display: flex;
  border: 1px solid #c8d1d9;
  border-radius: 8px;
  overflow: hidden;
}

.toggle-btn {
  padding: 7px 18px;
  border: none;
  border-radius: 0;
  background: #ffffff;
  color: #64717d;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  height: auto;
  transition: background 0.15s, color 0.15s;
}

.toggle-btn + .toggle-btn {
  border-left: 1px solid #c8d1d9;
}

.toggle-btn:hover:not(.active) {
  background: #eef1f4;
  color: #182026;
}

.toggle-btn.active {
  background: #4f7ef8;
  color: #ffffff;
  font-weight: 600;
}
</style>

<template>
    <model-editor-base
        :loading="loading"
        :dialog="dialog"
        @save="saveObject"
        @delete="deleteObject"
        @close="emit('close'); editingObjChanged = false"
        :is-update="isUpdate()"
        :is-changed="editingObjChanged"
        :model-class="modelClass"
        :object-name="editingObjName()"
        :editing-object="editingObj">
        <v-card-text>
            <v-form :disabled="loading">
                <v-text-field :label="$t('Name')" v-model="editingObj.name"></v-text-field>
                <v-textarea :label="$t('Description')" v-model="editingObj.description"></v-textarea>

                <v-select
                    label="Runtime"
                    :items="runtimeOptions"
                    item-title="title"
                    item-value="value"
                    v-model="runtime"
                    class="mb-2"
                ></v-select>

                <template v-if="runtime === 'litellm'">
                    <v-text-field :label="$t('APIKey')" v-model="editingObj.apiKey" type="password"></v-text-field>
                    <v-combobox :label="$t('Model')" :items="aiModels" v-model="editingObj.modelName" hide-details></v-combobox>
                    <p class="mt-2 mb-2">{{ $t('AiModelHelp') }} <a href="https://docs.litellm.ai/docs/providers" target="_blank">LiteLLM</a></p>
                    <v-checkbox :label="$t('LogCredits')" :hint="$t('LogCreditsHelp')" v-model="editingObj.logCreditCost"
                                v-if="useUserPreferenceStore().userSettings.user.isSuperuser" persistent-hint class="mb-2"></v-checkbox>
                    <v-text-field :label="$t('Url')" v-model="editingObj.url" :hint="$t('AllowedAiUrlHelp')"></v-text-field>
                </template>

                <template v-else>
                    <v-text-field
                        label="Model (optional)"
                        v-model="subscriptionModel"
                        placeholder="default"
                        hint="Leave blank to use the runtime's default model."
                        persistent-hint
                        class="mb-3"
                    ></v-text-field>

                    <template v-if="runtime === 'claude-code'">
                        <v-text-field
                            label="Claude setup token"
                            v-model="editingObj.apiKey"
                            type="password"
                            autocomplete="new-password"
                            hint="Generate with: claude setup-token. Existing saved tokens are never returned to the browser."
                            persistent-hint
                        ></v-text-field>
                        <v-alert type="info" variant="tonal" class="mt-3">
                            Uses your Claude subscription through an isolated Claude Code runtime. Tandoor credits are not consumed.
                        </v-alert>
                    </template>

                    <template v-if="runtime === 'codex'">
                        <v-alert type="info" variant="tonal" class="mb-3">
                            Uses a dedicated Codex credential for this Tandoor instance. It never mounts your host <code>~/.codex</code> directory and does not use an OpenAI API key.
                        </v-alert>

                        <template v-if="!isUpdate()">
                            <v-alert type="warning" variant="tonal">Save this provider first, then reopen it to sign in with ChatGPT.</v-alert>
                        </template>
                        <template v-else>
                            <v-chip :color="runtimeStatus?.connected ? 'success' : undefined" class="mb-3">
                                {{ runtimeStatus?.connected ? 'ChatGPT connected' : 'ChatGPT not connected' }}
                            </v-chip>
                            <div class="d-flex ga-2 flex-wrap mb-3">
                                <v-btn size="small" variant="tonal" :loading="runtimeBusy" @click="startCodexLogin">Sign in with ChatGPT</v-btn>
                                <v-btn size="small" variant="tonal" :loading="runtimeBusy" @click="loadRuntimeStatus">Refresh status</v-btn>
                                <v-btn size="small" variant="text" :loading="runtimeBusy" @click="logoutCodex" v-if="runtimeStatus?.connected">Disconnect</v-btn>
                            </div>
                            <v-alert v-if="runtimeStatus?.login?.verification_url" type="warning" variant="tonal" class="mb-3">
                                Open <a :href="runtimeStatus.login.verification_url" target="_blank">{{ runtimeStatus.login.verification_url }}</a>
                                and enter code <strong>{{ runtimeStatus.login.user_code }}</strong>.
                                <div class="mt-1">Status: {{ runtimeStatus.login.status }}</div>
                            </v-alert>
                        </template>
                    </template>

                    <v-btn v-if="isUpdate()" size="small" variant="outlined" :loading="runtimeBusy" class="mt-3" @click="testRuntime">Test runtime</v-btn>
                    <v-alert v-if="runtimeMessage" :type="runtimeMessageType" variant="tonal" class="mt-3">{{ runtimeMessage }}</v-alert>
                </template>

                <v-checkbox :label="$t('Global')" :hint="$t('GlobalHelp')" v-model="globalProvider"
                            v-if="useUserPreferenceStore().userSettings.user.isSuperuser" persistent-hint class="mb-2"></v-checkbox>
            </v-form>
        </v-card-text>
    </model-editor-base>
</template>

<script setup lang="ts">
import {onBeforeUnmount, onMounted, PropType, ref, watch} from "vue";
import {AiProvider} from "@/openapi";
import ModelEditorBase from "@/components/model_editors/ModelEditorBase.vue";
import {useModelEditorFunctions} from "@/composables/useModelEditorFunctions";
import {useUserPreferenceStore} from "@/stores/UserPreferenceStore.ts";
import {getCookie} from "@/utils/cookie.ts";

const props = defineProps({
    item: {type: {} as PropType<AiProvider>, required: false, default: null},
    itemId: {type: [Number, String], required: false, default: undefined},
    itemDefaults: {type: {} as PropType<AiProvider>, required: false, default: {} as AiProvider},
    dialog: {type: Boolean, default: false}
})

const emit = defineEmits(['create', 'save', 'delete', 'close', 'changedState'])
const {setupState, deleteObject, saveObject, isUpdate, editingObjName, loading, editingObj, editingObjChanged, modelClass} = useModelEditorFunctions<AiProvider>('AiProvider', emit)

const aiModels = ref(['gemini/gemini-2.5-pro', 'gemini/gemini-2.5-flash', 'gemini/gemini-2.5-flash-lite', 'gpt-5', 'gpt-5-mini', 'gpt-5-nano'])
const runtimeOptions = [
    {title: 'LiteLLM / API', value: 'litellm'},
    {title: 'OpenAI Codex / ChatGPT subscription', value: 'codex'},
    {title: 'Claude Code / subscription', value: 'claude-code'},
]
const runtime = ref<'litellm' | 'codex' | 'claude-code'>('litellm')
const subscriptionModel = ref('')
const globalProvider = ref(false)
const initializing = ref(false)
const runtimeBusy = ref(false)
const runtimeStatus = ref<any>(null)
const runtimeMessage = ref('')
const runtimeMessageType = ref<'success' | 'error' | 'info'>('info')
let statusTimer: ReturnType<typeof setInterval> | null = null

watch([() => props.item, () => props.itemId], () => initializeEditor())

watch(() => globalProvider.value, () => {
    if (initializing.value) return
    editingObj.value.space = globalProvider.value ? undefined : useUserPreferenceStore().activeSpace.id!
})

watch(runtime, (next, previous) => {
    if (initializing.value || next === previous) return
    runtimeStatus.value = null
    runtimeMessage.value = ''
    subscriptionModel.value = ''
    if (next === 'litellm') {
        editingObj.value.modelName = ''
        editingObj.value.apiKey = ''
        editingObj.value.url = undefined
        editingObj.value.logCreditCost = true
    } else {
        editingObj.value.modelName = `${next}/default`
        editingObj.value.apiKey = ''
        editingObj.value.url = undefined
        editingObj.value.logCreditCost = false
    }
})

watch(subscriptionModel, value => {
    if (initializing.value || runtime.value === 'litellm') return
    editingObj.value.modelName = `${runtime.value}/${value.trim() || 'default'}`
})

onMounted(() => initializeEditor())
onBeforeUnmount(() => stopStatusPolling())

function initializeEditor() {
    initializing.value = true
    setupState(props.item, props.itemId, {
        itemDefaults: props.itemDefaults,
        newItemFunction: () => {
            editingObj.value.logCreditCost = true
            editingObj.value.space = useUserPreferenceStore().activeSpace.id!
        },
    }).then(() => {
        globalProvider.value = editingObj.value.space == undefined
        const model = editingObj.value.modelName || ''
        if (model.startsWith('codex/')) runtime.value = 'codex'
        else if (model.startsWith('claude-code/')) runtime.value = 'claude-code'
        else runtime.value = 'litellm'
        subscriptionModel.value = runtime.value === 'litellm' ? '' : (model.split('/', 2)[1] === 'default' ? '' : model.split('/', 2)[1] || '')
        initializing.value = false
        if (isUpdate() && runtime.value !== 'litellm') loadRuntimeStatus()
    }).catch(() => { initializing.value = false })
}

async function runtimeRequest(action: string, method: 'GET' | 'POST' = 'GET', body?: object) {
    if (!editingObj.value.id) throw new Error('Save the provider before configuring its runtime.')
    const headers: Record<string, string> = {'Accept': 'application/json'}
    if (method !== 'GET') {
        headers['Content-Type'] = 'application/json'
        const csrf = getCookie('csrftoken')
        if (csrf) headers['X-CSRFToken'] = csrf
    }
    const response = await fetch(`/api/ai-provider/${editingObj.value.id}/${action}/`, {
        method,
        credentials: 'same-origin',
        headers,
        body: body ? JSON.stringify(body) : undefined,
    })
    const data = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(data?.msg || data?.detail || data?.error || `Request failed (${response.status})`)
    return data
}

async function loadRuntimeStatus() {
    if (!isUpdate() || runtime.value === 'litellm') return
    try {
        const loginId = runtimeStatus.value?.login?.id
        const suffix = loginId ? `?login_id=${encodeURIComponent(loginId)}` : ''
        runtimeStatus.value = await runtimeRequest(`runtime_status/${suffix}`)
        if (runtimeStatus.value?.login?.status === 'connected') {
            runtimeStatus.value.connected = true
            runtimeMessage.value = 'ChatGPT sign-in complete.'
            runtimeMessageType.value = 'success'
            stopStatusPolling()
        } else if (runtimeStatus.value?.login?.status === 'failed') {
            runtimeMessage.value = runtimeStatus.value.login.error || 'ChatGPT sign-in failed.'
            runtimeMessageType.value = 'error'
            stopStatusPolling()
        }
    } catch (err: any) {
        runtimeMessage.value = err?.message || String(err)
        runtimeMessageType.value = 'error'
    }
}

async function startCodexLogin() {
    runtimeBusy.value = true
    runtimeMessage.value = ''
    try {
        const login = await runtimeRequest('codex_login', 'POST')
        runtimeStatus.value = {runtime: 'codex', connected: false, login}
        startStatusPolling()
    } catch (err: any) {
        runtimeMessage.value = err?.message || String(err)
        runtimeMessageType.value = 'error'
    } finally {
        runtimeBusy.value = false
    }
}

async function logoutCodex() {
    runtimeBusy.value = true
    try {
        await runtimeRequest('codex_logout', 'POST')
        runtimeStatus.value = {runtime: 'codex', connected: false}
        runtimeMessage.value = 'Codex disconnected.'
        runtimeMessageType.value = 'success'
    } catch (err: any) {
        runtimeMessage.value = err?.message || String(err)
        runtimeMessageType.value = 'error'
    } finally {
        runtimeBusy.value = false
    }
}

async function testRuntime() {
    runtimeBusy.value = true
    runtimeMessage.value = ''
    try {
        const result = await runtimeRequest('runtime_test', 'POST')
        runtimeMessage.value = result?.ok ? 'Runtime test passed.' : 'Runtime test returned an unexpected response.'
        runtimeMessageType.value = result?.ok ? 'success' : 'error'
    } catch (err: any) {
        runtimeMessage.value = err?.message || String(err)
        runtimeMessageType.value = 'error'
    } finally {
        runtimeBusy.value = false
    }
}

function startStatusPolling() {
    stopStatusPolling()
    statusTimer = setInterval(loadRuntimeStatus, 2000)
}

function stopStatusPolling() {
    if (statusTimer) clearInterval(statusTimer)
    statusTimer = null
}
</script>

<style scoped></style>

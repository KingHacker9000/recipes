<template>
    <v-container>
        <v-row>
            <v-col cols="12" lg="10" offset-lg="1">
                <v-card class="mb-4">
                    <v-card-title>Social Recipe Inbox</v-card-title>
                    <v-card-subtitle>Paste a TikTok, Instagram Reel, or YouTube URL. Processing happens in the background.</v-card-subtitle>
                    <v-card-text>
                        <v-form @submit.prevent="queueImport">
                            <v-text-field
                                v-model="sourceUrl"
                                label="TikTok / Instagram / YouTube URL"
                                placeholder="https://..."
                                :disabled="submitting"
                                clearable
                            />
                            <v-btn type="submit" color="primary" :loading="submitting" :disabled="!sourceUrl.trim()">
                                Add to inbox
                            </v-btn>
                            <v-btn class="ml-2" variant="text" :loading="loading" @click="loadJobs">Refresh</v-btn>
                        </v-form>
                        <v-alert v-if="error" class="mt-4" type="warning" variant="tonal">{{ error }}</v-alert>
                    </v-card-text>
                </v-card>

                <v-alert v-if="!loading && jobs.length === 0" type="info" variant="tonal">
                    No social recipe imports yet.
                </v-alert>

                <v-card v-for="job in jobs" :key="job.id" class="mb-4">
                    <v-card-title class="d-flex align-center">
                        <span>{{ job.extraction?.title || job.caption || platformLabel(job.platform) }}</span>
                        <v-spacer />
                        <v-chip :color="statusColor(job.status)" size="small">{{ statusLabel(job.status) }}</v-chip>
                    </v-card-title>
                    <v-card-subtitle>
                        {{ platformLabel(job.platform) }}<span v-if="job.creator"> · {{ job.creator }}</span>
                    </v-card-subtitle>
                    <v-card-text>
                        <a :href="job.canonical_url || job.source_url" target="_blank" rel="noopener noreferrer">
                            {{ job.canonical_url || job.source_url }}
                        </a>

                        <v-alert v-if="job.error" class="mt-3" type="warning" variant="tonal">{{ job.error }}</v-alert>

                        <template v-if="job.status === 'ready' || job.status === 'failed'">
                            <div class="text-subtitle-2 mt-4 mb-2">Review extraction before saving</div>
                            <v-textarea
                                v-model="drafts[job.id]"
                                rows="14"
                                auto-grow
                                spellcheck="false"
                                label="Recipe JSON"
                            />
                            <div v-if="job.confidence !== null && job.confidence !== undefined" class="text-caption mb-2">
                                Extraction confidence: {{ Math.round(Number(job.confidence) * 100) }}%
                            </div>
                        </template>
                    </v-card-text>
                    <v-card-actions>
                        <v-btn
                            v-if="job.status === 'failed'"
                            variant="tonal"
                            :loading="busyJob === job.id"
                            @click="retryJob(job)"
                        >Retry</v-btn>
                        <v-btn
                            v-if="job.status === 'ready' || job.status === 'failed'"
                            color="success"
                            :loading="busyJob === job.id"
                            @click="saveJob(job)"
                        >Save Recipe</v-btn>
                        <v-btn
                            v-if="job.recipe_id"
                            color="primary"
                            :to="{name: 'RecipeViewPage', params: {id: job.recipe_id}}"
                        >Open Recipe</v-btn>
                    </v-card-actions>
                </v-card>
            </v-col>
        </v-row>
    </v-container>
</template>

<script setup lang="ts">
import {onBeforeUnmount, onMounted, reactive, ref} from 'vue'

interface SocialImportJob {
    id: number
    source_url: string
    canonical_url: string
    platform: string
    creator: string
    caption: string
    status: string
    extraction: Record<string, any>
    confidence: number | string | null
    error: string
    recipe_id: number | null
}

const jobs = ref<SocialImportJob[]>([])
const sourceUrl = ref('')
const loading = ref(false)
const submitting = ref(false)
const busyJob = ref<number | null>(null)
const error = ref('')
const drafts = reactive<Record<number, string>>({})
let pollTimer: number | null = null

function csrfToken(): string {
    const entry = document.cookie.split('; ').find(row => row.startsWith('csrftoken='))
    return entry ? decodeURIComponent(entry.split('=').slice(1).join('=')) : ''
}

async function api(path: string, options: RequestInit = {}) {
    const headers = new Headers(options.headers || {})
    headers.set('Accept', 'application/json')
    if (options.body) headers.set('Content-Type', 'application/json')
    const token = csrfToken()
    if (token) headers.set('X-CSRFToken', token)
    const response = await fetch(path, {...options, headers, credentials: 'same-origin'})
    const body = response.status === 204 ? null : await response.json().catch(() => null)
    if (!response.ok) {
        throw new Error(body?.msg || body?.detail || `Request failed (${response.status})`)
    }
    return body
}

function syncDraft(job: SocialImportJob) {
    if (!(job.id in drafts) || job.status !== 'ready') {
        drafts[job.id] = JSON.stringify(job.extraction || {}, null, 2)
    }
}

async function loadJobs() {
    loading.value = true
    error.value = ''
    try {
        const data = await api('/api/social-import/')
        jobs.value = data || []
        jobs.value.forEach(syncDraft)
        schedulePoll()
    } catch (err: any) {
        error.value = err?.message || String(err)
    } finally {
        loading.value = false
    }
}

async function queueImport() {
    const url = sourceUrl.value.trim()
    if (!url) return
    submitting.value = true
    error.value = ''
    try {
        const job = await api('/api/social-import/', {
            method: 'POST',
            body: JSON.stringify({source_url: url}),
        })
        sourceUrl.value = ''
        jobs.value.unshift(job)
        syncDraft(job)
        schedulePoll()
    } catch (err: any) {
        error.value = err?.message || String(err)
    } finally {
        submitting.value = false
    }
}

async function retryJob(job: SocialImportJob) {
    busyJob.value = job.id
    error.value = ''
    try {
        const updated = await api(`/api/social-import/${job.id}/retry/`, {method: 'POST'})
        replaceJob(updated)
        schedulePoll()
    } catch (err: any) {
        error.value = err?.message || String(err)
    } finally {
        busyJob.value = null
    }
}

async function saveJob(job: SocialImportJob) {
    busyJob.value = job.id
    error.value = ''
    try {
        let extraction
        try {
            extraction = JSON.parse(drafts[job.id] || '{}')
        } catch {
            throw new Error('Recipe JSON is invalid. Fix it before saving.')
        }
        const result = await api(`/api/social-import/${job.id}/save/`, {
            method: 'POST',
            body: JSON.stringify({extraction}),
        })
        replaceJob(result.job)
    } catch (err: any) {
        error.value = err?.message || String(err)
    } finally {
        busyJob.value = null
    }
}

function replaceJob(updated: SocialImportJob) {
    const index = jobs.value.findIndex(job => job.id === updated.id)
    if (index >= 0) jobs.value[index] = updated
    else jobs.value.unshift(updated)
    syncDraft(updated)
}

function schedulePoll() {
    if (pollTimer !== null) window.clearTimeout(pollTimer)
    const pending = jobs.value.some(job => ['queued', 'acquiring', 'extracting', 'saving'].includes(job.status))
    if (pending) pollTimer = window.setTimeout(loadJobs, 5000)
}

function platformLabel(platform: string) {
    return ({tiktok: 'TikTok', instagram: 'Instagram', youtube: 'YouTube'} as Record<string, string>)[platform] || platform
}

function statusLabel(status: string) {
    return ({
        queued: 'Queued',
        acquiring: 'Getting post',
        extracting: 'Extracting recipe',
        ready: 'Ready for review',
        saving: 'Saving',
        saved: 'Saved',
        failed: 'Needs attention',
    } as Record<string, string>)[status] || status
}

function statusColor(status: string) {
    if (status === 'saved') return 'success'
    if (status === 'ready') return 'primary'
    if (status === 'failed') return 'warning'
    return 'info'
}

onMounted(() => {
    const query = new URLSearchParams(window.location.search)
    sourceUrl.value = query.get('url') || query.get('text') || ''
    loadJobs()
})

onBeforeUnmount(() => {
    if (pollTimer !== null) window.clearTimeout(pollTimer)
})
</script>

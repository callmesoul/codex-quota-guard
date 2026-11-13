<script setup lang="ts">
import { computed } from 'vue'
import { Button } from '@/components/ui/button'
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from '@/components/ui/empty'
import { ScrollArea } from '@/components/ui/scroll-area'
import { handoffLabel } from '@/lib/board'
import type { AskState } from '@/lib/board'
import { renderMd } from '@/lib/markdown'
import type { Handoff, Project } from '@/lib/types'
import { cn } from '@/lib/utils'
import { Inbox, MessageSquare } from '@lucide/vue'

const props = defineProps<{
  project: Project | null
  handoff: Handoff | null
  choices: Handoff[]
  selectedHandoff: string | null
  askState: AskState
  askNotice: string
  askPrompt: string
  /** True when the board runs inside DSH and the ask opens a session. */
  dsh?: boolean
}>()

const emit = defineEmits<{
  selectHandoff: [id: string]
  ask: [handoff: Handoff]
  copy: [handoff: Handoff]
}>()

const body = computed(() => {
  const handoff = props.handoff
  if (!handoff) return ''
  if (handoff.has_markdown && handoff.markdown) return renderMd(handoff.markdown)
  return ''
})

const fallback = computed(() => {
  const handoff = props.handoff
  if (!handoff) return ''
  return handoff.goal || handoff.reason || 'No HANDOFF.md beside handoff.json.'
})

const emptyTitle = computed(() => {
  if (!props.project) return 'Pick a project to read its handoff documents.'
  if (props.project.handoff_count > 0) return 'Select a handoff in the session list.'
  return `${props.project.name} has no .handoff documents yet.`
})

const createdLabel = computed(() => {
  const iso = props.handoff?.created_at
  if (!iso) return ''
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const d = new Date(t)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
})

const busy = computed(() => props.askState === 'sending')
const askLabel = computed(() => {
  if (busy.value) return props.dsh ? 'Starting…' : 'Asking…'
  return props.dsh ? '发起 DSH 会话' : 'Ask Codex'
})
const askHint = computed(() =>
  props.dsh ? 'Open a DSH session that continues this handoff' : 'Ask Codex to continue this handoff',
)
/** A failed ask still needs a manual path, so the prompt becomes selectable. */
const showPrompt = computed(() => props.askState === 'failed' && Boolean(props.askPrompt))
</script>

<template>
  <section class="min-w-0">
    <div class="hb-card">
      <template v-if="!handoff">
        <Empty class="border-0">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Inbox />
            </EmptyMedia>
            <EmptyTitle>{{ emptyTitle }}</EmptyTitle>
            <EmptyDescription>
              Handoffs live in <code>.handoff/&lt;utc&gt;-&lt;slug&gt;/HANDOFF.md</code> inside each project.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      </template>

      <template v-else>
        <header class="hb-doc-head">
          <div v-if="choices.length > 1" class="hb-choices">
            <Button
              v-for="choice in choices"
              :key="choice.id"
              :variant="choice.id === selectedHandoff ? 'secondary' : 'ghost'"
              size="xs"
              :class="cn('max-w-44', choice.id === selectedHandoff && 'ring-1 ring-border')"
              @click="emit('selectHandoff', choice.id)"
            >
              <span class="truncate">{{ handoffLabel(choice) }}</span>
            </Button>
          </div>

          <div class="flex min-w-0 items-start gap-2">
            <div class="min-w-0 flex-1">
              <h2 class="hb-doc-title" :title="handoff.id">
                {{ handoff.goal || handoffLabel(handoff) }}
              </h2>
              <p class="hb-doc-sub" :title="handoff.id">
                <span class="truncate">{{ handoffLabel(handoff) }}</span>
                <span class="hb-sep">·</span>
                <span class="shrink-0">{{ createdLabel }}</span>
              </p>
            </div>
            <div class="flex shrink-0 items-center gap-1">
              <Button
                variant="ghost"
                size="xs"
                title="Copy the takeover prompt to the clipboard"
                @click="emit('copy', handoff)"
              >
                Copy
              </Button>
              <Button size="xs" class="shrink-0" :disabled="busy" :title="askHint" @click="emit('ask', handoff)">
                <MessageSquare data-icon="inline-start" />
                {{ askLabel }}
              </Button>
            </div>
          </div>

          <p
            v-if="askNotice"
            class="hb-doc-notice"
            :data-state="askState"
            role="status"
            aria-live="polite"
          >
            {{ askNotice }}
          </p>

          <div class="hb-doc-tags">
            <span v-if="handoff.status" class="hb-tag">{{ handoff.status }}</span>
            <span v-if="handoff.verdict" class="hb-tag" :class="`hb-tag-${handoff.verdict.toLowerCase()}`">
              {{ handoff.verdict }}
            </span>
            <span v-if="handoff.branch" class="hb-tag hb-tag-mono">
              {{ handoff.branch }}@{{ handoff.head || '?' }}
            </span>
            <span v-if="handoff.dirty" class="hb-tag hb-tag-warn" title="Working tree has uncommitted changes">
              uncommitted
            </span>
            <span v-if="handoff.agent" class="hb-tag">{{ handoff.agent }}</span>
            <span v-if="!handoff.has_markdown" class="hb-tag hb-tag-warn">no HANDOFF.md</span>
          </div>
        </header>

        <ScrollArea class="hb-doc-body">
          <div class="px-3 py-2.5">
            <article v-if="body" class="hb-md" v-html="body" />
            <p v-else class="text-sm text-muted-foreground">{{ fallback }}</p>
          </div>
        </ScrollArea>

        <footer class="hb-doc-foot">
          <textarea
            v-if="showPrompt"
            class="hb-doc-prompt"
            readonly
            rows="3"
            aria-label="Takeover prompt"
            :value="askPrompt"
            @focus="($event.target as HTMLTextAreaElement).select()"
          />
          <span v-else class="truncate" :title="handoff.dir">{{ handoff.dir }}</span>
        </footer>
      </template>
    </div>
  </section>
</template>

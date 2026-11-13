<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import HandoffDoc from '@/components/board/HandoffDoc.vue'
import ProjectList from '@/components/board/ProjectList.vue'
import SessionList from '@/components/board/SessionList.vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useBoard } from '@/lib/board'
import { dshBridge, fetchBoard } from '@/lib/data'
import { ListFilter, RefreshCw, Search } from '@lucide/vue'

const {
  query,
  onlyHandoffs,
  selectedProject,
  selectedSession,
  selectedHandoff,
  stats,
  currentProject,
  currentHandoff,
  visibleProjects,
  pinnedProject,
  orphanHandoffs,
  visibleSessions,
  handoffChoices,
  selectProject,
  selectSession,
  selectHandoff,
  askCodex,
  copyPrompt,
  currentAskPrompt,
  askState,
  askNotice,
  load,
} = useBoard()

/** True inside the DSH plugin: the ask button opens a harness session instead. */
const dsh = computed(() => Boolean(dshBridge()) || Boolean(window.__HANDOFF_BOARD_DSH__?.boardUrl))

const plural = (n: number, one: string) => (n === 1 ? one : `${one}s`)

const docSpan = computed(() => (visibleProjects.value.length ? 'hb-doc' : 'hb-doc hb-doc-empty'))

const loading = ref(false)

async function reload() {
  if (loading.value) return
  loading.value = true
  try {
    load(await fetchBoard())
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void reload()
})
</script>

<template>
  <div class="flex flex-col gap-3">
    <header class="flex flex-wrap items-center gap-x-3 gap-y-2">
      <div class="flex min-w-0 items-baseline gap-2.5">
        <h1 class="text-base font-semibold tracking-tight">Handoff Board</h1>
        <p class="flex flex-wrap items-center gap-1 text-[11px] text-muted-foreground">
          <span class="hb-chip">
            <b>{{ stats.projects }}</b> {{ plural(stats.projects, 'project') }}
          </span>
          <span class="hb-chip">
            <b>{{ stats.sessions }}</b> {{ plural(stats.sessions, 'session') }}
          </span>
          <span class="hb-chip" :class="stats.handoffs > 0 && 'hb-chip-accent'">
            <b>{{ stats.handoffs }}</b> {{ plural(stats.handoffs, 'handoff') }}
          </span>
        </p>
      </div>

      <div class="ml-auto flex flex-1 items-center justify-end gap-2">
        <div class="relative w-full max-w-56 min-w-32">
          <Search
            class="pointer-events-none absolute top-1/2 left-2 size-3.5 -translate-y-1/2 text-muted-foreground"
          />
          <Input
            v-model="query"
            type="search"
            placeholder="Filter projects, sessions, handoffs"
            class="h-8 pl-7 text-xs"
            aria-label="Filter projects, sessions and handoffs"
          />
        </div>
        <Button
          variant="ghost"
          size="xs"
          :aria-pressed="onlyHandoffs"
          :class="onlyHandoffs && 'bg-accent text-accent-foreground hover:bg-accent/80'"
          title="Show only projects and sessions that have a handoff document"
          @click="onlyHandoffs = !onlyHandoffs"
        >
          <ListFilter data-icon="inline-start" />
          handoffs only
        </Button>
        <Button
          variant="ghost"
          size="xs"
          :disabled="loading"
          title="Reload the handoff board"
          @click="reload"
        >
          <RefreshCw data-icon="inline-start" :class="loading && 'animate-spin'" />
          refresh
        </Button>
      </div>
    </header>

    <div
      class="hb-board grid grid-cols-[minmax(120px,0.8fr)_minmax(150px,1.05fr)_minmax(280px,2.55fr)] gap-3 max-panel:grid-cols-2 max-narrow:grid-cols-1"
    >
      <ProjectList
        :projects="visibleProjects"
        :selected="selectedProject"
        :pinned="pinnedProject"
        @select="selectProject"
      />
      <SessionList
        :project="currentProject"
        :sessions="visibleSessions"
        :orphans="orphanHandoffs"
        :selected-session="selectedSession"
        :selected-handoff="selectedHandoff"
        @select-session="selectSession"
        @select-handoff="selectHandoff"
      />
      <HandoffDoc
        :class="docSpan"
        :project="currentProject"
        :handoff="currentHandoff"
        :choices="handoffChoices"
        :selected-handoff="selectedHandoff"
        :ask-state="askState"
        :ask-notice="askNotice"
        :ask-prompt="currentAskPrompt"
        :dsh="dsh"
        @select-handoff="selectHandoff"
        @ask="askCodex"
        @copy="copyPrompt"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty'
import { ScrollArea } from '@/components/ui/scroll-area'
import { ago, sessionLabel } from '@/lib/board'
import type { Handoff, Project, Session } from '@/lib/types'
import { cn } from '@/lib/utils'
import { FileText } from '@lucide/vue'

defineProps<{
  project: Project | null
  sessions: Session[]
  orphans: Handoff[]
  selectedSession: string | null
  selectedHandoff: string | null
}>()

const emit = defineEmits<{
  selectSession: [id: string]
  selectHandoff: [id: string]
}>()
</script>

<template>
  <section class="flex min-w-0 flex-col gap-1.5">
    <h2 class="hb-col-title">
      Sessions
      <span v-if="project" class="hb-col-count">{{ sessions.length }}</span>
      <span v-if="project" class="hb-col-project" :title="project.path">{{ project.name }}</span>
    </h2>
    <ScrollArea class="hb-list">
      <div v-if="!project" class="px-2 py-3 text-xs text-muted-foreground">Pick a project.</div>
      <ul v-else class="flex flex-col gap-px pr-1">
        <li v-for="handoff in orphans" :key="`orphan-${handoff.id}`">
          <button
            type="button"
            :aria-current="handoff.id === selectedHandoff ? 'true' : undefined"
            :class="cn('hb-row', handoff.id === selectedHandoff && 'hb-row-on')"
            @click="emit('selectHandoff', handoff.id)"
          >
            <span class="flex w-full min-w-0 items-center gap-1.5">
              <FileText class="hb-glyph" />
              <span class="min-w-0 flex-1 truncate text-[13px] leading-5">
                {{ handoff.goal || handoff.id }}
              </span>
            </span>
            <span class="hb-meta">
              <span class="hb-tag">handoff only</span>
              <span class="truncate">{{ ago(handoff.created_at) }}</span>
            </span>
          </button>
        </li>
        <li v-for="session in sessions" :key="session.id">
          <button
            type="button"
            :aria-current="session.id === selectedSession ? 'true' : undefined"
            :class="cn('hb-row', session.id === selectedSession && 'hb-row-on')"
            @click="emit('selectSession', session.id)"
          >
            <span class="flex w-full min-w-0 items-center gap-1.5">
              <span class="min-w-0 flex-1 truncate text-[13px] leading-5">{{ sessionLabel(session) }}</span>
              <span
                v-if="(session.handoff_ids ?? []).length"
                class="hb-dot"
                :title="`${session.handoff_ids.length} handoff document(s)`"
              >
                {{ session.handoff_ids.length }}
              </span>
            </span>
            <span class="hb-meta">
              <span class="truncate">{{ session.id.slice(0, 8) }}</span>
              <span class="hb-sep">·</span>
              <span class="truncate">{{ ago(session.updated_at) }}</span>
            </span>
          </button>
        </li>
        <li v-if="!sessions.length && !orphans.length">
          <Empty class="border-0 p-4">
            <EmptyHeader>
              <EmptyTitle>No sessions.</EmptyTitle>
              <EmptyDescription>This project has no rollout records here.</EmptyDescription>
            </EmptyHeader>
          </Empty>
        </li>
      </ul>
    </ScrollArea>
  </section>
</template>

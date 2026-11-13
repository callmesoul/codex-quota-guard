<script setup lang="ts">
import { ScrollArea } from '@/components/ui/scroll-area'
import { ago } from '@/lib/board'
import type { Project } from '@/lib/types'
import { cn } from '@/lib/utils'

defineProps<{
  projects: Project[]
  selected: string | null
  pinned: boolean
}>()

const emit = defineEmits<{ select: [path: string] }>()
</script>

<template>
  <section class="flex min-w-0 flex-col gap-1.5">
    <h2 class="hb-col-title">
      Projects
      <span class="hb-col-count">{{ projects.length }}</span>
    </h2>
    <ScrollArea class="hb-list">
      <ul class="flex flex-col gap-px pr-1">
        <li v-if="!projects.length" class="px-2 py-3 text-xs text-muted-foreground">
          No projects matched.
        </li>
        <li v-for="project in projects" :key="project.path">
          <button
            type="button"
            :aria-current="project.path === selected ? 'true' : undefined"
            :class="
              cn(
                'hb-row',
                project.path === selected && 'hb-row-on',
                pinned && project.path === selected && 'opacity-60',
              )
            "
            @click="emit('select', project.path)"
          >
            <span class="flex w-full min-w-0 items-center gap-1.5">
              <span class="min-w-0 flex-1 truncate text-[13px] leading-5">{{ project.name }}</span>
              <span
                v-if="project.handoff_count"
                class="hb-dot"
                :title="`${project.handoff_count} handoff document(s)`"
              >
                {{ project.handoff_count }}
              </span>
            </span>
            <span class="hb-meta">
              <span>{{ project.session_count }}s</span>
              <span class="hb-sep">·</span>
              <span class="truncate">{{ ago(project.updated_at) }}</span>
            </span>
          </button>
        </li>
      </ul>
    </ScrollArea>
  </section>
</template>

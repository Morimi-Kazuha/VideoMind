<template>
  <div class="academy-root">
    <header class="lab-header">
      <div class="lab-header__inner">
        <a class="lab-brand" href="/" aria-label="VideoMind 媒体库">
          <span class="lab-brand__mark"><AcademyMark /></span>
          <span>
            VideoMind
            <small>ACADEMY MEDIA LAB</small>
          </span>
        </a>
        <nav class="lab-nav" aria-label="Design lab sections">
          <a href="#workstation">WORKSTATION</a>
          <a href="#foundation">FOUNDATION</a>
          <a href="#controls">COMPONENTS</a>
          <a href="#media-ai">MEDIA + AI</a>
        </nav>
        <div class="lab-header__right">
          <AcademyStatus label="LAB" value="ONLINE" state="success" />
          <a class="lab-return" href="/">
            OPEN APP
            <span aria-hidden="true">↗</span>
          </a>
        </div>
      </div>
    </header>

    <main class="lab-main">
      <section class="lab-intro" aria-labelledby="lab-title">
        <div class="lab-intro__copy">
          <p class="lab-eyebrow">
            <span class="lab-square"></span>
            DESIGN SYSTEM / UI-0 / NIGHT SESSION
          </p>
          <h1 id="lab-title">
            Pixel Future
            <br />
            <em>Academy</em>
          </h1>
          <p class="lab-intro__desc">
            面向视频研究的安静而精确的视觉语言。让媒体、证据与每一帧之间的关系更清晰。
          </p>
          <div class="lab-intro__links">
            <a
              href="#workstation"
              class="academy-button academy-button--primary"
            >
              查看工作台
              <span aria-hidden="true">↓</span>
            </a>
            <a href="#foundation" class="lab-text-link">VIEW FOUNDATIONS ↗</a>
          </div>
        </div>
        <div class="lab-intro__card" aria-label="Design lab metadata">
          <div class="lab-intro__scene" aria-hidden="true"></div>
          <div class="lab-intro__card-top">
            <span>TERMINAL ACCESS</span>
            <AcademyMark class="lab-intro__seal" />
          </div>
          <div class="lab-intro__coordinates">
            <span>A–04</span>
            <span>/</span>
            <span>07</span>
          </div>
          <div class="lab-intro__card-bottom">
            <span>
              LAB NODE
              <br />
              <strong>MEDIA RESEARCH</strong>
            </span>
            <span>
              SESSION
              <br />
              <strong>21:48 / NIGHT</strong>
            </span>
            <span>
              INTERFACE
              <br />
              <strong>UI-0</strong>
            </span>
          </div>
        </div>
      </section>

      <section
        id="workstation"
        class="lab-section"
        aria-labelledby="workstation-title"
      >
        <div class="lab-section__heading">
          <div>
            <p class="lab-eyebrow">01 / APPLICATION COMPOSITION</p>
            <h2 id="workstation-title">视频研究工作台</h2>
          </div>
          <span class="lab-section__aside">
            DESIGN PROTOTYPE · NO LIVE MEDIA
          </span>
        </div>
        <div class="workstation">
          <div class="workstation__top">
            <div>
              <AcademyMark class="workstation__mark" />
              <strong>
                MEDIA LAB
                <span>// A-04</span>
              </strong>
            </div>
            <div class="workstation__top-center">
              PROJECT 02
              <span>/</span>
              ARCHIVE 07
              <span>/</span>
              ANALYSIS
            </div>
            <AcademyStatus label="SYSTEM" value="READY" state="success" />
          </div>
          <div class="workstation__body">
            <aside
              class="workstation__rail"
              aria-label="Prototype project navigation"
            >
              <p class="rail-label">
                PROJECT SPACE
                <span>02</span>
              </p>
              <div class="rail-project is-current">
                <AcademyMark class="rail-project__mark" />
                <span>
                  Night archive
                  <small>MEDIA SET / 04</small>
                </span>
              </div>
              <div class="rail-project">
                <span class="rail-project__glyph">◫</span>
                <span>
                  Lecture reel
                  <small>MEDIA SET / 03</small>
                </span>
              </div>
              <p class="rail-label rail-label--spaced">LIBRARY</p>
              <div class="rail-link">
                ⌑
                <span>Media files</span>
                <small>04</small>
              </div>
              <div class="rail-link">
                ▤
                <span>Evidence</span>
                <small>18</small>
              </div>
              <div class="rail-link">
                ⊞
                <span>Transcript</span>
              </div>
              <div class="workstation__rail-bottom">
                CAMPUS NETWORK
                <span>●</span>
                <br />
                RESEARCH NODE / 07
              </div>
            </aside>
            <div class="workstation__media">
              <div class="media-heading">
                <div>
                  <p class="lab-eyebrow">MEDIA VIEWER // 01</p>
                  <h3>校园天文台 / 夜间学习记录</h3>
                </div>
                <span class="academy-badge academy-badge--cyan">
                  SOURCE VIDEO
                </span>
              </div>
              <div class="media-viewport">
                <div class="campus-scene" aria-hidden="true"></div>
                <span class="media-viewport__corner">
                  CAM 01
                  <span>·</span>
                  ARCHIVE PLAYBACK
                </span>
                <span class="media-viewport__tag">OBSERVATORY WALKWAY</span>
                <div class="media-viewport__controls">
                  <span class="media-play" aria-hidden="true">▶</span>
                  <span>{{ activeTime }}</span>
                  <span class="media-viewport__scrub">
                    <i
                      :style="{
                        width: `${((activeSegment + 1) / segments.length) * 100}%`,
                      }"
                    ></i>
                  </span>
                  <span>24:00</span>
                  <span>▣</span>
                </div>
              </div>
              <div class="media-timeline-heading">
                <span>TIMELINE / EVIDENCE MAP</span>
                <span>FRAME {{ activeFrame }}</span>
              </div>
              <AcademyTimeline v-model="activeSegment" :segments="segments" />
              <div class="media-footnote">
                <span>CH 02 / EVENING STUDY</span>
                <span>
                  <i class="mini-light"></i>
                  ASR INDEXED &nbsp;
                  <i class="mini-light mini-light--violet"></i>
                  OCR INDEXED
                </span>
              </div>
            </div>
            <aside class="workstation__agent">
              <div class="workstation__agent-head">
                <div>
                  <p class="lab-eyebrow">AI ASSISTANT // ACTIVE</p>
                  <h3>研究记录</h3>
                </div>
                <AcademyMark class="agent-sigil" />
              </div>
              <div class="workstation__agent-body">
                <AgentMessage sender="user" time="21:48">
                  <p>视频中什么时候提到天文台？画面里出现了什么？</p>
                </AgentMessage>
                <div class="agent-step">
                  <span class="pixel-cluster" aria-hidden="true"></span>
                  EVIDENCE RETRIEVED
                  <span>02 / 02</span>
                </div>
                <AgentMessage sender="agent" time="21:49">
                  <p>
                    天文台在
                    <strong>13:42</strong>
                    被提及。画面显示北侧步道和亮起的塔楼。另一处线索出现在
                    <strong>18:06</strong>
                    附近。
                  </p>
                </AgentMessage>
                <EvidenceCard
                  index="07"
                  source="ASR + OCR"
                  time="00:13:42"
                  frame="02381"
                  excerpt="……天文台走廊就在北侧步道尽头。"
                  @open="activeSegment = 13"
                />
              </div>
              <div class="workstation__agent-prompt">
                <span>ASK ABOUT THIS MEDIA</span>
                <span aria-hidden="true">↗</span>
              </div>
            </aside>
          </div>
          <div class="workstation__footer">
            <div>
              <span class="footer-cross">✦</span>
              VideoMind / ACADEMY RESEARCH TERMINAL
            </div>
            <div>
              ASR
              <i class="mini-light"></i>
              &nbsp; OCR
              <i class="mini-light"></i>
              &nbsp; INDEX
              <i class="mini-light"></i>
            </div>
            <div>FRAME {{ activeFrame }}</div>
          </div>
        </div>
      </section>

      <section
        id="foundation"
        class="lab-section"
        aria-labelledby="foundation-title"
      >
        <div class="lab-section__heading">
          <div>
            <p class="lab-eyebrow">02 / FOUNDATION</p>
            <h2 id="foundation-title">A system with restraint</h2>
          </div>
          <span class="lab-section__aside">DARK BY DEFAULT · TOKEN DRIVEN</span>
        </div>
        <div class="foundation-grid">
          <AcademyPanel label="COLOR / SEMANTIC TOKENS" index="01">
            <div class="swatch-grid">
              <div
                v-for="swatch in swatches"
                :key="swatch.token"
                class="swatch"
              >
                <span
                  class="swatch__color"
                  :style="{ background: swatch.color }"
                ></span>
                <div>
                  <strong>{{ swatch.name }}</strong>
                  <code>{{ swatch.color }}</code>
                </div>
              </div>
            </div>
          </AcademyPanel>
          <div class="foundation-stack">
            <AcademyPanel label="TYPE / THREE ROLES" index="02">
              <div class="type-sample type-sample--body">
                <span>INTERFACE / BODY</span>
                <strong>清晰的文字，是可信证据的起点。</strong>
                <small>Space Grotesk · Noto Sans SC · system sans</small>
              </div>
              <div class="type-sample type-sample--mono">
                <span>SYSTEM / TELEMETRY</span>
                <strong>FRAME 02381 &nbsp; 00:13:42</strong>
                <small>Consolas · monospace</small>
              </div>
              <div class="type-sample type-sample--display">
                <span>DISPLAY / IDENTITY</span>
                <strong>VideoMind // A-04</strong>
                <small>Compact display lettering; never for paragraphs</small>
              </div>
            </AcademyPanel>
            <AcademyPanel label="GEOMETRY / SPACING" index="03">
              <div class="geometry-row">
                <div class="geometry-chip">
                  01
                  <span>CRISP EDGE</span>
                </div>
                <div class="geometry-chip geometry-chip--notch">
                  02
                  <span>PIXEL NOTCH</span>
                </div>
                <div class="geometry-chip geometry-chip--active">
                  03
                  <span>ACTIVE BORDER</span>
                </div>
              </div>
              <div class="spacing-row">
                <span
                  v-for="space in spacing"
                  :key="space"
                  :style="{ width: `${space}px` }"
                ></span>
              </div>
              <p class="panel-caption">
                4 · 8 · 12 · 16 · 24 · 32 · 48 px &nbsp; / &nbsp; radius 2–4 px
              </p>
            </AcademyPanel>
          </div>
        </div>
      </section>

      <section
        id="controls"
        class="lab-section"
        aria-labelledby="controls-title"
      >
        <div class="lab-section__heading">
          <div>
            <p class="lab-eyebrow">03 / COMPONENT LIBRARY</p>
            <h2 id="controls-title">Controls and feedback</h2>
          </div>
          <span class="lab-section__aside">KEYBOARD READY · FOCUS VISIBLE</span>
        </div>
        <div class="component-grid">
          <AcademyPanel label="INTERACTIVE CONTROLS" index="04">
            <div class="control-group">
              <span class="control-label">BUTTONS / ACTION HIERARCHY</span>
              <div class="button-row">
                <AcademyButton
                  variant="primary"
                  @click="notice = 'Prototype action acknowledged'"
                >
                  开始分析
                  <span aria-hidden="true">↗</span>
                </AcademyButton>
                <AcademyButton
                  @click="notice = 'Draft saved in this design preview'"
                >
                  保存草稿
                </AcademyButton>
                <AcademyButton variant="ghost">更多操作</AcademyButton>
                <AcademyButton
                  variant="danger"
                  @click="notice = 'Danger action preview only'"
                >
                  删除
                </AcademyButton>
                <AcademyButton
                  aria-label="Open component note"
                  class="academy-button--icon"
                  @click="openDialog"
                >
                  ✧
                </AcademyButton>
                <AcademyButton disabled>暂不可用</AcademyButton>
              </div>
            </div>
            <div class="control-group">
              <span class="control-label">
                FIELDS / SEARCH &amp; ANNOTATION
              </span>
              <div class="field-grid">
                <label class="academy-field">
                  <span>SEARCH ARCHIVE</span>
                  <input
                    v-model="query"
                    type="search"
                    placeholder="Observatory, frame, transcript…"
                  />
                </label>
                <label class="academy-field">
                  <span>MEDIA SOURCE</span>
                  <select v-model="source">
                    <option>All sources</option>
                    <option>Transcript / ASR</option>
                    <option>Visual / OCR</option>
                  </select>
                </label>
              </div>
              <label class="academy-field academy-field--textarea">
                <span>RESEARCH NOTE</span>
                <textarea
                  v-model="note"
                  rows="3"
                  placeholder="Add a short research note…"
                ></textarea>
              </label>
            </div>
            <div class="control-group">
              <span class="control-label">TABS / KEYBOARD ARROWS</span>
              <AcademyTabs
                v-model="activeTab"
                label="Media information"
                panel-id="media-details"
                :items="tabs"
              />
              <div
                id="media-details"
                class="tab-content"
                role="tabpanel"
                :aria-labelledby="`media-details-${activeTab}`"
              >
                <span class="lab-square"></span>
                {{ tabDescriptions[activeTab] }}
              </div>
            </div>
            <div class="control-group control-group--last">
              <span class="control-label">
                BADGES / TOOLTIP / MENU / DIALOG
              </span>
              <div class="button-row">
                <span class="academy-badge">ARCHIVED</span>
                <span class="academy-badge academy-badge--cyan">OCR</span>
                <span class="academy-badge academy-badge--success">
                  VERIFIED
                </span>
                <span
                  class="academy-tooltip"
                  tabindex="0"
                  aria-describedby="evidence-tip"
                >
                  EVIDENCE INFO
                  <span id="evidence-tip" role="tooltip">
                    Evidence links remain tied to the source frame.
                  </span>
                </span>
                <details class="academy-dropdown">
                  <summary>
                    MORE OPTIONS
                    <span aria-hidden="true">⌄</span>
                  </summary>
                  <div>
                    <button
                      type="button"
                      @click="notice = 'Export is a preview only'"
                    >
                      Export notes
                    </button>
                    <button type="button" @click="openDialog">
                      About UI-0
                    </button>
                  </div>
                </details>
                <AcademyButton @click="openDialog">OPEN DIALOG</AcademyButton>
              </div>
            </div>
            <p v-if="notice" class="lab-notice" role="status">
              {{ notice }}
            </p>
          </AcademyPanel>
          <AcademyPanel label="SYSTEM FEEDBACK" index="05">
            <div class="status-grid">
              <AcademyStatus label="AI" value="就绪" state="success" />
              <AcademyStatus label="ASR" value="处理中" state="active" />
              <AcademyStatus label="OCR" value="待命" />
              <AcademyStatus label="索引" value="需关注" state="warning" />
            </div>
            <AcademyProgress
              label="ANALYSING MEDIA"
              :value="57"
              detail="FRAME 02491  /  ASR COMPLETE  /  INDEX PROCESSING"
            />
            <div class="feedback-grid">
              <div class="feedback-note feedback-note--success">
                <strong>✓ &nbsp; ARCHIVE READY</strong>
                <span>Source and timestamps are available.</span>
              </div>
              <div class="feedback-note feedback-note--warning">
                <strong>◇ &nbsp; INDEX PENDING</strong>
                <span>Transcript search may be incomplete.</span>
              </div>
              <div class="feedback-note feedback-note--error">
                <strong>× &nbsp; MEDIA UNAVAILABLE</strong>
                <span>Retry the source connection.</span>
              </div>
            </div>
            <div class="skeleton-stack" aria-label="Loading preview">
              <span></span>
              <span></span>
              <span></span>
            </div>
            <div class="academy-empty">
              <AcademyMark class="academy-empty__glyph" />
              <div>
                <strong>NO MATCHING FRAMES</strong>
                <p>Try a different timestamp or search term.</p>
              </div>
            </div>
          </AcademyPanel>
        </div>
      </section>

      <section
        id="media-ai"
        class="lab-section lab-section--last"
        aria-labelledby="media-ai-title"
      >
        <div class="lab-section__heading">
          <div>
            <p class="lab-eyebrow">04 / MEDIA + INTELLIGENCE</p>
            <h2 id="media-ai-title">Evidence is the interface</h2>
          </div>
          <span class="lab-section__aside">PROTOTYPE COMPOSITIONS</span>
        </div>
        <div class="showcase-grid">
          <AcademyPanel label="TIMELINE / DENSE MEDIA" index="06">
            <p class="panel-copy">
              Compact markers distinguish speech, visual text, and evidence
              ranges. Choose a block to move the prototype frame cursor.
            </p>
            <AcademyTimeline v-model="activeSegment" :segments="segments" />
            <div class="showcase-metadata">
              <span>
                ACTIVE FRAME
                <strong>{{ activeFrame }}</strong>
              </span>
              <span>
                POSITION
                <strong>{{ activeTime }}</strong>
              </span>
            </div>
          </AcademyPanel>
          <AcademyPanel label="EVIDENCE / ARCHIVE RECORD" index="07">
            <div class="evidence-pair">
              <EvidenceCard
                index="07"
                source="ASR"
                time="00:13:42"
                frame="02381"
                excerpt="…the observatory corridor opens just beyond the north walkway."
                @open="activeSegment = 13"
              />
              <EvidenceCard
                index="08"
                source="OCR"
                time="00:18:06"
                frame="03124"
                excerpt="NORTH OBSERVATORY / ACCESS A-04"
                @open="activeSegment = 18"
              />
            </div>
          </AcademyPanel>
          <AcademyPanel label="ASSISTANT / STRUCTURED CONVERSATION" index="08">
            <div class="message-stack">
              <AgentMessage sender="user" time="21:48">
                <p>
                  Summarize the observatory sequence with source references.
                </p>
              </AgentMessage>
              <div class="agent-step">
                <span class="pixel-cluster" aria-hidden="true"></span>
                RETRIEVING EVIDENCE
                <span class="agent-step__cursor" aria-hidden="true">▌</span>
              </div>
              <AgentMessage sender="agent" time="21:49">
                <p>
                  The north walkway appears at
                  <strong>13:42</strong>
                  ; the observatory sign is visible at
                  <strong>18:06</strong>
                  . Both references are linked to source frames.
                </p>
                <button
                  type="button"
                  class="academy-text-button"
                  @click="activeSegment = 13"
                >
                  EVIDENCE 07 ↗
                </button>
              </AgentMessage>
            </div>
          </AcademyPanel>
        </div>
      </section>
    </main>
    <footer class="lab-footer">
      <span>VideoMind / VISUAL SYSTEM UI-0</span>
      <span>PIXEL FUTURE ACADEMY · NIGHT LAB</span>
      <a href="/">RETURN TO APPLICATION ↗</a>
    </footer>
    <dialog
      ref="dialog"
      class="academy-dialog"
      aria-labelledby="dialog-title"
      @click="onDialogBackdrop"
    >
      <div>
        <p class="lab-eyebrow">DESIGN SYSTEM / UI-0</p>
        <h2 id="dialog-title">A working visual foundation</h2>
        <p>
          This page is an isolated visual prototype. Media, analysis, and
          project data here are illustrative and never call production APIs.
        </p>
        <AcademyButton variant="primary" @click="closeDialog">
          CLOSE TERMINAL
        </AcademyButton>
      </div>
    </dialog>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import './academy.css'
import AcademyButton from './AcademyButton.vue'
import AcademyPanel from './AcademyPanel.vue'
import AcademyStatus from './AcademyStatus.vue'
import AcademyProgress from './AcademyProgress.vue'
import AcademyTabs from './AcademyTabs.vue'
import AcademyTimeline from './AcademyTimeline.vue'
import EvidenceCard from './EvidenceCard.vue'
import AgentMessage from './AgentMessage.vue'
import AcademyMark from './AcademyMark.vue'

const swatches = [
  { name: 'VOID', token: 'bg-void', color: '#070A12' },
  { name: 'PANEL', token: 'bg-panel', color: '#10172A' },
  { name: 'RAISED', token: 'bg-panel-raised', color: '#141D32' },
  { name: 'BLUE', token: 'academy-blue', color: '#68A8FF' },
  { name: 'CYAN', token: 'academy-cyan', color: '#67E8F9' },
  { name: 'VIOLET', token: 'academy-violet', color: '#A78BFA' },
  { name: 'TEXT', token: 'text-primary', color: '#EAF2FF' },
  { name: 'BORDER', token: 'border-normal', color: '#273650' },
]
const spacing = [4, 8, 12, 16, 24, 32, 48]
const segmentKinds = [
  'quiet',
  'asr',
  'asr',
  'ocr',
  'quiet',
  'asr',
  'evidence',
  'evidence',
  'asr',
  'quiet',
  'ocr',
  'asr',
  'quiet',
  'evidence',
  'evidence',
  'asr',
  'ocr',
  'quiet',
  'evidence',
  'asr',
  'asr',
  'ocr',
  'quiet',
  'asr',
]
const segments = segmentKinds.map((kind, index) => ({
  kind,
  label: kind.toUpperCase(),
  channels:
    kind === 'evidence'
      ? ['asr', ...(index % 2 === 0 ? ['ocr'] : []), 'evidence']
      : kind === 'quiet'
        ? []
        : [kind],
}))
const activeSegment = ref(13)
const activeTime = computed(() =>
  activeSegment.value === 13
    ? '13:42'
    : activeSegment.value === 18
      ? '18:06'
      : `${String(activeSegment.value).padStart(2, '0')}:00`,
)
const activeFrame = computed(() =>
  activeSegment.value === 18
    ? '03124'
    : String(2140 + activeSegment.value * 18 + 7).padStart(5, '0'),
)
const tabs = [
  { label: 'TRANSCRIPT', value: 'transcript' },
  { label: 'FRAMES', value: 'frames' },
  { label: 'METADATA', value: 'metadata' },
]
const tabDescriptions = {
  transcript: 'Time-aligned speech remains readable and searchable.',
  frames: 'Visual cues are indexed to source frames.',
  metadata: 'Source identity, duration, and processing state live together.',
}
const activeTab = ref('transcript')
const query = ref('')
const source = ref('All sources')
const note = ref('')
const notice = ref('')
const dialog = ref(null)
function openDialog() {
  dialog.value?.showModal()
}
function closeDialog() {
  dialog.value?.close()
}
function onDialogBackdrop(event) {
  if (event.target === dialog.value) closeDialog()
}
</script>

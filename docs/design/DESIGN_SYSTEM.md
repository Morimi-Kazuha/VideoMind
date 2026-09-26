# DOVideo — Pixel Future Academy

UI-0 is the visual foundation for DOVideo's Vue client. Its live, isolated showcase is `/design-lab` (`cd client && npm run dev`). The existing application at `/` is unchanged by this phase. All media, research notes, status values, and evidence shown in the lab are illustrative.

## Vision and product metaphor

A serious AI video workstation in a quiet near-future academy media laboratory at night. The academy appears through lab identifiers, an original pixel-built campus scene, archive language, and observatory references. The video and evidence remain the main content. Aim for roughly 70% professional application, 20% pixel-derived geometry, and 10% atmosphere. The interface should be usable without knowing the fiction.

## Structure

- `client/src/main.js` selects the lab only for `/design-lab`; all other paths continue to mount the existing application. There is no router dependency.
- `client/src/design/academy.css` owns the tokens and visual grammar under `.academy-root`. The production `App.vue` stylesheet is loaded only with the production route.
- `client/src/design/DesignLab.vue` composes the workstation and component examples. It imports no production API or demo business module.
- `client/src/design/night-campus.svg` is the original, locally authored scene used in the mock video and faintly echoed on the entry plate.
- Small Vue primitives in `client/src/design/` are ready for later migration of production pages. Native controls use the same `.academy-field` class rather than one wrapper per HTML element.

## Color tokens

All lab colors live on `.academy-root`. Use tokens in future components; add a semantic token before duplicating a color literal.

| Role     | Tokens                                                                                               | Use                                           |
| -------- | ---------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| Surfaces | `--bg-void`, `--bg-primary`, `--bg-secondary`, `--bg-panel`, `--bg-panel-raised`, `--bg-panel-hover` | Receding shell, working surfaces, hover       |
| Identity | `--academy-blue`, `--academy-cyan`, `--academy-violet`, `--academy-pink`                             | Selection, active media, AI, very rare warmth |
| Text     | `--text-primary`, `--text-secondary`, `--text-muted`                                                 | Reading, supporting copy, metadata            |
| Borders  | `--border-subtle`, `--border-normal`, `--border-active`                                              | Quiet structure, panels, focus                |
| Meaning  | `--success`, `--warning`, `--danger`                                                                 | State, never decoration alone                 |

Dark is the default. Blue and cyan guide attention; most of the canvas remains quiet navy. Avoid decorative glows and large gradients. Never communicate state with color alone: pair status lights with words.

## Typography and density

- Body and interface: Space Grotesk, Noto Sans SC, then system sans. Use normal readable sizes for responses, descriptions, forms, and long text.
- Telemetry: Consolas/Cascadia/system monospace for timestamps, frame IDs, statuses, labels, and metadata.
- Display: compact tracked monospace for DOVideo identity and tiny system labels. Do not use it for paragraphs.
- The spacing scale is 4, 8, 12, 16, 24, 32, and 48 pixels (`--space-*`). Workstation panels use compact padding, not marketing-card spacing.

## Geometry and pixel language

Panels use a thin technical border, 2–4 pixel corner radius at most, and a small cutout/notch only on selected surfaces. The repeated pixel grammar has three signatures: the D/frame/play mark, a seven-pixel cut corner on selected records, and multi-lane media blocks with a vertical frame cursor. Tiny four-pixel clusters signal AI processing. Reuse these few motifs instead of inventing a new border or sprite for each surface. Body copy, video controls, and dense evidence are rendered normally.

### DOVideo mark

`AcademyMark.vue` draws an abstract D around a pixel play arrow in a 20×20 grid. It works in the application identity, selected project, assistant header, and empty state. Keep it as a companion to ordinary labels; do not fill every panel with marks.

## Components

- `AcademyButton`: primary, secondary, ghost, danger, and icon use. It preserves native button behavior and a visible pressed displacement.
- `AcademyPanel`: consistent panel title, optional index, and body slots.
- `AcademyStatus`: text and semantic square light for success, active, idle, warning.
- `AcademyProgress`: stepped media progress with native progressbar semantics and a real value.
- `AcademyTabs`: keyboard arrow, Home, and End navigation with roving tab focus.
- `AcademyTimeline`: three aligned ASR/OCR/evidence lanes, compact pink evidence pixels, and a selectable cyan frame cursor. It is a visual prototype, not synchronized playback.
- `EvidenceCard`: source, timestamp, frame, excerpt, and an explicit source-frame action.
- `AgentMessage`: differentiated researcher and agent records with sender and time, without chat bubbles.
- Native `input`, `textarea`, `select`, `details`, and `dialog` provide field, menu, and modal foundations. The lab demonstrates tooltip, badge, divider, empty state, skeleton, feedback, header, and sidebar item treatments. Use native elements and shared CSS classes until a separate component API has a real reuse case.

## Media, AI, and evidence

The media viewer is the dominant surface. Its scene has block-built academy buildings, uneven illuminated windows, a research tower, an elevated walkway, distant lights, and a restrained violet-blue sky. The timeline carries separate evidence channels and exposes a discrete active position. Violet identifies the assistant/research thread; cyan represents data activity; pink is reserved for sparse evidence markers. An AI answer is a structured research record with linked evidence, not an isolated chatbot bubble. Evidence cards keep source type, time, and frame visible together. A future production integration must take all metadata from the real backend and preserve the existing source/provenance contract; the lab data must never flow into business routes.

## Motion and artwork

Motion is limited to a 1-pixel pressed shift, low-key active status pulse, and skeleton sweep. `prefers-reduced-motion` disables them. No typing delay or continuous glitch. The local SVG campus scene is isolated inside the lab's mock video viewer and faint entry plate. Later environmental art belongs in entry, onboarding, empty, or transitional surfaces, not behind playback, AI answers, or forms. Do not use third-party anime images as filler.

## Accessibility and responsive behavior

Focus is visible on interactive elements. Buttons, tabs, timeline segments, details, and the native dialog are keyboard operable. Text contrast uses the three text tiers against dark surfaces, and semantic states include labels. Decorative layers ignore pointer events. The workstation targets 1440×900, retains its media-first hierarchy at 1024 pixels, and stacks on narrower screens. The dense timeline can scroll horizontally on phones rather than shrinking blocks into unusable targets.

## Do / don't

| Do                                            | Don't                                        |
| --------------------------------------------- | -------------------------------------------- |
| Keep video and evidence prominent             | Fill panels with ornamental interface chrome |
| Use tokens and shared primitives              | Scatter one-off colors and radii             |
| Use academy cues in labels and atmosphere     | Roleplay every ordinary control              |
| Show state with text plus a small light       | Depend on glow or color alone                |
| Keep body text modern and readable            | Pixelate long answers or forms               |
| Preserve production API and policy boundaries | Wire mock lab data into real analysis routes |

## Extending the system

For a later production page, first reuse the tokens and the smallest appropriate primitive, then build the page around real media and evidence content. Keep route-level data and policy in the existing application layer. Add a new primitive only when at least two concrete uses need the same behavior. Validate at desktop and one smaller viewport, with keyboard focus and reduced motion. UI-0 deliberately stops before migrating the production application.

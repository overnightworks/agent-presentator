# Voice model requirements

Audience: the operator and the engineer implementing the admin Voice surface.
Altitude: precise behavior contract. This document is the single owner of the
adopted V1–V9 expectations; the [Voice picture](../mockups/README.md) is its
visual reference and does not claim implementation.

The operator had already spoken admin-only model controls, explicit download,
load, select, and sample listening through the private control path. The
remaining reversible defaults below were presented with examples,
counterexamples, and the head's dissent, then adopted under the standing
expectation-list rule after a response window exceeding two minutes without a
contrary reply. This records adoption provenance; it does not claim a new
explicit yes.

Piper and Chatterbox are supported baselines. Qwen3-TTS 0.6B, VoxCPM2, and
NVIDIA Magpie are the named candidate scope. No candidate is described as
installed or downloaded here: those states require runtime evidence under V9.
Piper's installed Thorsten voice remains German and must be labelled that way.

## Adopted expectations

### V1 · download without selection

An admin explicitly downloads a listed model; downloading does not select it.
Default: manual download, then Load.

- Example: download Qwen while Chatterbox remains active.
- Counterexample: opening Settings downloads weights or silently changes the
  voice.

### V2 · switch between sentences

Once loading succeeds, the next spoken sentence uses the selected voice; a
sentence already speaking finishes unchanged. Default: switch between
sentences.

- Example: finish the current Chatterbox sentence, then use Piper.
- Counterexample: change voice or sample rate halfway through the current
  sentence.

### V3 · visible switching state

New speech requested during a model change gets a visible busy or failure
state. Default: refuse while switching, without a hidden queue.

- Example: presenter text remains visible with an explicit speech failure.
- Counterexample: the answer waits silently behind a model load until timeout.

### V4 · honest load recovery

A failed load keeps the old voice when possible; if it had to be released,
show no active voice and offer Load again. Default: honest recovery state.

- Example: a failed baseline load leaves Chatterbox active; a failed GPU
  replacement may leave none.
- Counterexample: claim the old voice is active after its process was released.

### V5 · restore the selected voice

The speech service restores the selected voice after restart. Only a
never-selected instance uses its configured initial default; an
invalid/unloadable stored choice stays stored and shows no active voice.
Default: restore selection, no silent fallback.

- Example: select Piper, restart, get Piper; a failed stored choice is shown
  for recovery.
- Counterexample: restart silently changes the choice or overwrites a failed
  selection.

### V6 · admin-only controls

Only admins may view or change model controls or trigger samples, through the
existing authenticated lobby and private speech control path. Admin-only is
already ruled: there is no public or co-presenter control route.

- Example: an admin opens Voice; a non-admin gets 403.
- Counterexample: a visitor or unauthenticated co-presenter endpoint can load a
  model.

### V7 · change without removal

Choosing another model undoes a selection; downloaded weights stay and no
selection history or Delete control is added. Default: change back, retain
downloads.

- Example: load Chatterbox after trying Piper.
- Counterexample: offer a history rollback or remove model files automatically.

### V8 · current talk keeps synthesis

One sample uses the active voice and visibly waits if a talk owns synthesis.
Default: give the current talk priority.

- Example: the player shows Waiting until the current answer releases speech.
- Counterexample: a preview changes models secretly, overlaps samples, or
  interrupts the talk.

### V9 · truthful runtime state

Show actual active, downloaded, loading, failed, and unavailable states;
unimplemented candidates say not yet available. When service status cannot be
verified, hide the rows and offer Check again to refresh. Display declared
language and preset information without inventing progress, speed, or quality.
Default: truthful status and upstream facts.

- Example: installed German Piper is named accurately; a pending adapter has
  no fake Download button.
- Counterexample: promise native English Piper, invent a percentage, or
  advertise a benchmark never measured here.

## Boundary

This document rules intended behavior only. The Voice mockup remains future
surface evidence, and the named candidates remain unintegrated until runtime
evidence establishes their actual state. Functional implementation, runtime
configuration, persistence, and tests belong to parent #108 and later S0 work.

/**
 * Day 45 — request-generation guard (PR #91 F5).
 *
 * A minimal repository-consistent mechanism that prevents an older async
 * request from mutating state after a newer request has started: every
 * request calls `begin()` and receives its own `isCurrent()` — which
 * returns true only while its generation is the newest one begun.
 *
 * Presentation-layer concurrency guard only: it orders UI state updates
 * and grants no authorization of any kind.
 */

export function createRequestSequence() {
  let generation = 0;
  return {
    /** Register a new request; returns its `isCurrent()` checker. */
    begin() {
      generation += 1;
      const myGeneration = generation;
      return function isCurrent() {
        return myGeneration === generation;
      };
    },
  };
}

"use client";

import Link from "next/link";
import { motion, useReducedMotion } from "motion/react";
import { formatDay, formatKickoffTime, formatPercent } from "@/lib/format";
import {
  groupFixtures,
  groupFixturesByDay,
  moneylineProbabilities,
  preferredState,
} from "@/lib/platform";
import type { PlatformSnapshot } from "@/lib/types";

export function FixtureIndex({ snapshot }: { snapshot: PlatformSnapshot }) {
  const reducedMotion = useReducedMotion();
  const fixtures = groupFixtures(snapshot.states);
  const days = groupFixturesByDay(fixtures);

  return (
    <main className="page-shell fixtures-page">
      <section className="page-intro">
        <div>
          <p className="section-label">Upcoming fixtures</p>
          <h1>Matches</h1>
        </div>
        <p className="intro-copy">
          Regulation-time probabilities from the current validated model.
        </p>
      </section>

      {snapshot.is_stale && (
        <div className="snapshot-notice" role="status">
          The latest forecast snapshot is delayed. Displayed probabilities remain tied to their original cutoff.
        </div>
      )}

      <div className="fixture-days">
        {days.map(([day, dayFixtures], dayIndex) => (
          <motion.section
            className="fixture-day"
            key={day}
            initial={reducedMotion ? false : { opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.42, delay: dayIndex * 0.06, ease: [0.22, 1, 0.36, 1] }}
          >
            <div className="day-heading">
              <h2>{formatDay(dayFixtures[0].kickoff)}</h2>
              <span>{dayFixtures.length} {dayFixtures.length === 1 ? "match" : "matches"}</span>
            </div>
            <div className="fixture-list-clean">
              {dayFixtures.map((fixture) => {
                const state = preferredState(fixture);
                const probabilities = moneylineProbabilities(state);
                return (
                  <Link
                    className="fixture-row-clean"
                    href={`/matches/${encodeURIComponent(fixture.id)}`}
                    key={fixture.id}
                  >
                    <time className="fixture-row-time">{formatKickoffTime(fixture.kickoff)}</time>
                    <div className="fixture-row-match">
                      <span className="fixture-row-competition">{fixture.fixture.competition_name}</span>
                      <strong>{fixture.fixture.home_team_name}</strong>
                      <strong>{fixture.fixture.away_team_name}</strong>
                    </div>
                    <div className="fixture-probabilities" aria-label="Match result probabilities">
                      <Probability label="Home" value={probabilities.home_win} />
                      <Probability label="Draw" value={probabilities.draw} />
                      <Probability label="Away" value={probabilities.away_win} />
                    </div>
                    <span className="fixture-row-arrow" aria-hidden="true">›</span>
                  </Link>
                );
              })}
            </div>
          </motion.section>
        ))}
      </div>
    </main>
  );
}

function Probability({ label, value }: { label: string; value: number | null }) {
  return (
    <span>
      <small>{label}</small>
      <strong>{formatPercent(value)}</strong>
    </span>
  );
}

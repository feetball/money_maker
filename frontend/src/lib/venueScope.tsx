/**
 * `VenueScope` marks a subtree as belonging to one paper account (COINBASE_CONTRACT §14).
 * Inside it, DataTable adds a leading "Venue" column, KpiTile shows a VenueBadge, and
 * toasts / confirm dialogs raised from it carry the venue badge (unless a call passes
 * its own `venue`, or `null` to opt out). App.tsx wraps the Kalshi section in
 * `<VenueScope venue="kalshi">`; Coinbase pages may wrap themselves the same way.
 *
 * Kept free of other app imports so toast/confirm/ui can use it without import cycles.
 */
import { createContext, useContext, type ReactNode } from "react";
import type { Venue } from "../components/Venue";

const VenueScopeContext = createContext<Venue | null>(null);

export function VenueScope({ venue, children }: { venue: Venue; children: ReactNode }) {
  return <VenueScopeContext.Provider value={venue}>{children}</VenueScopeContext.Provider>;
}

/** The venue of the enclosing VenueScope, or null outside every scope (Overview, shell). */
export const useVenueScope = (): Venue | null => useContext(VenueScopeContext);

/**
 * Resolve an optional `venue` prop against the scope: `undefined` = inherit the scope,
 * `null` = explicitly none, a venue = that venue.
 */
export function useResolvedVenue(prop: Venue | null | undefined): Venue | null {
  const scope = useVenueScope();
  return prop === undefined ? scope : prop;
}

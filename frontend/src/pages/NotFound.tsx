import { Link, useLocation } from "react-router";
import { PageHeader } from "../components/ui";
import { venueFromPath, VENUES } from "../components/Venue";

export function NotFound() {
  const loc = useLocation();
  const venue = venueFromPath(loc.pathname);
  return (
    <div className="page">
      <PageHeader title="Page not found" subtitle={<span className="mono">{loc.pathname}</span>} />
      <p>
        There is no page at this address.{" "}
        {venue ? (
          <>
            <Link to={VENUES[venue].basePath}>Back to the {VENUES[venue].name} dashboard</Link> or the <Link to="/">Overview</Link>.
          </>
        ) : (
          <Link to="/">Back to the Overview</Link>
        )}
      </p>
    </div>
  );
}

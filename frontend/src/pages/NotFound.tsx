import { Link, useLocation } from "react-router";
import { PageHeader } from "../components/ui";

export function NotFound() {
  const loc = useLocation();
  return (
    <div className="page">
      <PageHeader title="Page not found" subtitle={<span className="mono">{loc.pathname}</span>} />
      <p>
        There is no page at this address. <Link to="/">Back to the dashboard</Link>.
      </p>
    </div>
  );
}

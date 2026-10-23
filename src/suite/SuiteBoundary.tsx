import { Component } from "react";
import type { ReactNode } from "react";

/** `label` names the screen in the error sentence; the default keeps the Suite tab's wording. */
export class SuiteBoundary extends Component<{ children: ReactNode; label?: string }, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return <div role="alert">
        <p>{this.props.label ?? "The Suite screen"} hit an unexpected error. Use Advanced &gt; Analyze, or reload the page.</p>
        <button type="button" onClick={() => this.setState({ hasError: false })}>Try again</button>
      </div>;
    }
    return this.props.children;
  }
}

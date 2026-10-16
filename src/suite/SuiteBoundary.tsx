import { Component } from "react";
import type { ReactNode } from "react";

export class SuiteBoundary extends Component<{ children: ReactNode }, { hasError: boolean }> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  render() {
    if (this.state.hasError) {
      return <div role="alert">
        <p>The Suite screen hit an unexpected error. Use the Analyze tab, or reload the page.</p>
        <button type="button" onClick={() => this.setState({ hasError: false })}>Try again</button>
      </div>;
    }
    return this.props.children;
  }
}

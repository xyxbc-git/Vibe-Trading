import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

interface Props {
  children: ReactNode;
  fallbackTitle?: string;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false, error: null };

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("[ErrorBoundary]", error, info.componentStack);
  }

  handleRetry = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      return (
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <AlertTriangle size={40} className="text-jarvis-yellow mb-4" />
          <h2 className="text-lg font-medium text-jarvis-text mb-2">
            {this.props.fallbackTitle ?? "页面加载异常"}
          </h2>
          <p className="text-sm text-jarvis-text-secondary mb-4 max-w-md">
            {this.state.error?.message ?? "发生了未知错误"}
          </p>
          <button
            onClick={this.handleRetry}
            className="btn-primary flex items-center gap-2"
          >
            <RefreshCw size={14} />
            重新加载
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}

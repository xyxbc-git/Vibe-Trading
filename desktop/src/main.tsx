import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { SymbolProvider } from "./hooks/useSymbol";
import "./styles/globals.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <BrowserRouter>
      <SymbolProvider>
        <App />
      </SymbolProvider>
    </BrowserRouter>
  </StrictMode>,
);

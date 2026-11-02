import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { App } from "./App";
import { captureInviteToken } from "./pages/AcceptInvitation";
import { createQueryClient } from "./queryClient";
import "./styles.css";

const queryClient = createQueryClient();

captureInviteToken(); // before the first render, so sign-in cannot drop it

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);

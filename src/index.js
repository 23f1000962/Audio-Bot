import { Container, getContainer } from "@cloudflare/containers";

const MODEL_VERSION = "0.3.0";

export class AudioBotContainer extends Container {
  defaultPort = 8080;
  // Telegram polling is a long-running process. Do not stop it merely because
  // the container has not received an HTTP request recently.
  async onActivityExpired() {
    console.log("Container idle timer reached; keeping Telegram bot alive.");
  }

  constructor(ctx, env) {
    super(ctx, env);
    this.envVars = {
      BOT_TOKEN: env.BOT_TOKEN,
      PORT: "8080",
    };
  }

  onStart() {
    console.log(`Audio Bot v${MODEL_VERSION} container started`);
  }

  onStop(params) {
    console.log(
      `Audio Bot v${MODEL_VERSION} container stopped`,
      params?.exitCode,
      params?.reason
    );
  }

  onError(error) {
    console.error(`Audio Bot v${MODEL_VERSION} container error`, error);
  }
}

async function startBot(env) {
  const container = getContainer(env.AUDIO_BOT_CONTAINER, "telegram-bot");
  await container.startAndWaitForPorts();
  return container;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === "/") {
      return new Response(
        `Audio Bot v${MODEL_VERSION} is deployed on Cloudflare Containers.\n`,
        { status: 200 }
      );
    }

    if (url.pathname === "/health") {
      return new Response(
        JSON.stringify({
          status: "ok",
          version: MODEL_VERSION,
          platform: "cloudflare-containers",
        }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        }
      );
    }

    const container = await startBot(env);
    return container.fetch(request);
  },

  async scheduled(_controller, env) {
    // Re-start the Telegram polling container if it was stopped/restarted.
    await startBot(env);
    console.log(`Audio Bot v${MODEL_VERSION} keep-alive check completed`);
  },
};

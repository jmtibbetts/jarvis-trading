type Toast = { id: number; message: string; tone: "good" | "bad" | "neutral" };

let nextId = 1;

class ToastStore {
  items = $state<Toast[]>([]);

  push(message: string, tone: Toast["tone"] = "neutral") {
    const id = nextId++;
    this.items = [...this.items, { id, message, tone }];
    setTimeout(() => {
      this.items = this.items.filter((t) => t.id !== id);
    }, 4000);
  }

  ok(message: string) {
    this.push(message, "good");
  }
  err(message: string) {
    this.push(message, "bad");
  }
}

export const toastStore = new ToastStore();

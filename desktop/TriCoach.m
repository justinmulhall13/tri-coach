// Tri Coach — native macOS shell (Objective-C / Cocoa + WebKit).
// A chrome-less WKWebView window that renders the local dashboard
// (served by the com.tricoach.dashboard launchd agent) as a real app.
#import <Cocoa/Cocoa.h>
#import <WebKit/WebKit.h>

// Point at the cloud backend so the Mac and phone share ONE database (synced).
// (Was http://127.0.0.1:8770 — the local server; now the Fly deployment.)
static NSString *const kURL = @"https://tri-coach-jm.fly.dev";

@interface AppDelegate : NSObject <NSApplicationDelegate, WKNavigationDelegate>
@property (strong) NSWindow *window;
@property (strong) WKWebView *web;
@property (assign) BOOL retrying;
@end

@implementation AppDelegate

- (void)applicationDidFinishLaunching:(NSNotification *)note {
    WKWebViewConfiguration *cfg = [[WKWebViewConfiguration alloc] init];
    self.web = [[WKWebView alloc] initWithFrame:NSZeroRect configuration:cfg];
    self.web.navigationDelegate = self;
    [self.web setValue:@NO forKey:@"drawsBackground"];  // no white flash; page bg shows

    NSRect frame = NSMakeRect(0, 0, 1200, 860);
    NSWindowStyleMask mask = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
        NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable |
        NSWindowStyleMaskFullSizeContentView;
    self.window = [[NSWindow alloc] initWithContentRect:frame styleMask:mask
                                                backing:NSBackingStoreBuffered defer:NO];
    self.window.title = @"Tri Coach";
    self.window.titlebarAppearsTransparent = YES;
    self.window.titleVisibility = NSWindowTitleHidden;
    self.window.backgroundColor = [NSColor colorWithRed:8/255.0 green:11/255.0 blue:16/255.0 alpha:1]; // --bg
    [self.window setFrameAutosaveName:@"TriCoachMain"];
    [self.window center];
    self.window.contentView = self.web;
    [self.window makeKeyAndOrderFront:nil];

    [self buildMenu];
    [self loadDashboard];
    [NSApp setActivationPolicy:NSApplicationActivationPolicyRegular];
    [NSApp activateIgnoringOtherApps:YES];
}

- (void)loadDashboard {
    // Ignore local cache so a redeploy (new features like the Nutrition tab) is
    // picked up instead of WKWebView serving a stale index.html.
    NSURLRequest *req = [NSURLRequest requestWithURL:[NSURL URLWithString:kURL]
                                        cachePolicy:NSURLRequestReloadIgnoringLocalCacheData
                                    timeoutInterval:30];
    [self.web loadRequest:req];
}

// Server not up yet → themed placeholder + retry.
- (void)showLoading {
    NSString *html = @"<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<style>html,body{height:100%;margin:0;background:#080b10;color:#8fa0b4;"
        "font-family:-apple-system,system-ui,sans-serif;display:flex;align-items:center;"
        "justify-content:center;flex-direction:column;gap:16px}"
        ".dot{width:12px;height:12px;border-radius:50%;background:#22d3ee;"
        "animation:p 1s ease-in-out infinite}"
        "@keyframes p{0%,100%{opacity:.3;transform:scale(.85)}50%{opacity:1;transform:scale(1.15)}}"
        ".t{font-size:13px;letter-spacing:.08em;text-transform:uppercase}</style></head>"
        "<body><div class='dot'></div><div class='t'>Starting Tri Coach…</div></body></html>";
    [self.web loadHTMLString:html baseURL:nil];
}

- (void)scheduleRetry {
    if (self.retrying) return;
    self.retrying = YES;
    [self showLoading];
    dispatch_after(dispatch_time(DISPATCH_TIME_NOW, (int64_t)(1.5 * NSEC_PER_SEC)),
                   dispatch_get_main_queue(), ^{
        self.retrying = NO;
        [self loadDashboard];
    });
}

- (void)webView:(WKWebView *)w didFailProvisionalNavigation:(WKNavigation *)n withError:(NSError *)e {
    [self scheduleRetry];
}
- (void)webView:(WKWebView *)w didFailNavigation:(WKNavigation *)n withError:(NSError *)e {
    [self scheduleRetry];
}

- (void)buildMenu {
    NSMenu *main = [[NSMenu alloc] init];

    NSMenuItem *appItem = [[NSMenuItem alloc] init];
    [main addItem:appItem];
    NSMenu *appMenu = [[NSMenu alloc] init];
    [appMenu addItemWithTitle:@"Reload" action:@selector(reload) keyEquivalent:@"r"];
    [appMenu addItem:[NSMenuItem separatorItem]];
    [appMenu addItemWithTitle:@"Hide Tri Coach" action:@selector(hide:) keyEquivalent:@"h"];
    [appMenu addItemWithTitle:@"Quit Tri Coach" action:@selector(terminate:) keyEquivalent:@"q"];
    appItem.submenu = appMenu;

    NSMenuItem *winItem = [[NSMenuItem alloc] init];
    [main addItem:winItem];
    NSMenu *winMenu = [[NSMenu alloc] initWithTitle:@"Window"];
    [winMenu addItemWithTitle:@"Minimize" action:@selector(performMiniaturize:) keyEquivalent:@"m"];
    [winMenu addItemWithTitle:@"Close" action:@selector(performClose:) keyEquivalent:@"w"];
    winItem.submenu = winMenu;

    NSApp.mainMenu = main;
}

- (void)reload { [self.web reloadFromOrigin]; }
- (BOOL)applicationShouldTerminateAfterLastWindowClosed:(NSApplication *)s { return YES; }
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        NSApplication *app = [NSApplication sharedApplication];
        AppDelegate *delegate = [[AppDelegate alloc] init];
        app.delegate = delegate;
        [app run];
    }
    return 0;
}

use std::fs;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::{Duration, Instant};

use anyhow::{Context, Result};
use clap::Parser;
use reqwest::Client;
use serde::Deserialize;

#[derive(Parser)]
#[command(
    name = "agn-health",
    about = "Simple AGN infrastructure health checker"
)]
struct Args {
    /// Path to the monitoring config JSON
    #[arg(long)]
    config: PathBuf,
}

#[derive(Deserialize)]
struct Config {
    endpoints: Vec<EndpointConfig>,
    timeout_sec: u64,
}

#[derive(Deserialize)]
struct EndpointConfig {
    name: String,
    url: String,
}

struct EndpointReport {
    name: String,
    ok: bool,
    status_text: String,
    latency_ms: Option<u128>,
}

#[tokio::main]
async fn main() -> ExitCode {
    match run().await {
        Ok(all_ok) if all_ok => ExitCode::SUCCESS,
        Ok(_) => ExitCode::FAILURE,
        Err(err) => {
            eprintln!("error: {err}");
            ExitCode::FAILURE
        }
    }
}

async fn run() -> Result<bool> {
    let args = Args::parse();
    let contents = fs::read_to_string(&args.config)
        .with_context(|| format!("reading config {}", args.config.display()))?;
    let config: Config =
        serde_json::from_str(&contents).context("parsing configuration as JSON")?;

    let timeout = Duration::from_secs(config.timeout_sec);
    let client = Client::builder()
        .timeout(timeout)
        .build()
        .context("building HTTP client")?;

    let mut tasks = Vec::with_capacity(config.endpoints.len());
    for endpoint in config.endpoints {
        let client = client.clone();
        tasks.push(tokio::spawn(async move { check_endpoint(endpoint, client).await }));
    }

    let mut all_ok = true;
    let mut reports = Vec::with_capacity(tasks.len());
    for task in tasks {
        let report = task.await.context("joining endpoint task")?;
        all_ok &= report.ok;
        reports.push(report);
    }

    print_reports(&reports);
    Ok(all_ok)
}

fn print_reports(reports: &[EndpointReport]) {
    const NAME_WIDTH: usize = 20;
    const STATUS_WIDTH: usize = 12;

    println!(
        "{:<NAME_WIDTH$} {:<STATUS_WIDTH$} LATENCY",
        "NAME",
        "STATUS"
    );
    for report in reports {
        let latency = report
            .latency_ms
            .map(|ms| format!("{ms}ms"))
            .unwrap_or_else(|| "-".to_string());
        println!(
            "{:<NAME_WIDTH$} {:<STATUS_WIDTH$} {}",
            report.name, report.status_text, latency
        );
    }
}

async fn check_endpoint(endpoint: EndpointConfig, client: Client) -> EndpointReport {
    let start = Instant::now();

    match client.get(&endpoint.url).send().await {
        Ok(response) => {
            let status = response.status().as_u16();
            EndpointReport {
                name: endpoint.name,
                ok: status == 200,
                status_text: format!("{} {}", if status == 200 { "✓" } else { "✗" }, status),
                latency_ms: Some(start.elapsed().as_millis()),
            }
        }
        Err(err) if err.is_timeout() => EndpointReport {
            name: endpoint.name,
            ok: false,
            status_text: "✗ timeout".to_string(),
            latency_ms: None,
        },
        Err(err) => EndpointReport {
            name: endpoint.name,
            ok: false,
            status_text: format!(
                "✗ {}",
                err.status()
                    .map(|status| status.as_u16().to_string())
                    .unwrap_or_else(|| "error".to_string())
            ),
            latency_ms: Some(start.elapsed().as_millis()),
        },
    }
}

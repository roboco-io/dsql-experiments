# frozen_string_literal: true

require "date"
require "yaml"

# Reports live in docs/_experiments/<lang>/eNNN.md. Korean is the source language; English (the site default,
# served at the root) and Japanese are translations that must carry the same facts and metadata.
LANGS = {
  "en" => { prefix: "", summary: "Results summary", production: "Production readiness", limit: 1300,
            sections: ["Results summary", "Test conditions", "Performance results", "Development and operations",
                       "Cost", "Conclusions and limitations", "Cleanup record"],
            placeholder: "This experiment has not run yet" },
  "ko" => { prefix: "/ko", summary: "결과 요약", production: "프로덕션 사용 관점", limit: 500,
            sections: ["결과 요약", "실행 조건", "성능 결과", "개발·운영 편의성", "비용", "결론과 한계", "정리 기록"],
            placeholder: "아직 실험을 실행하지 않았습니다" },
  "ja" => { prefix: "/ja", summary: "結果の要約", production: "本番利用の観点", limit: 700,
            sections: ["結果の要約", "実行条件", "性能結果", "開発・運用のしやすさ", "費用", "結論と限界", "後片付けの記録"],
            placeholder: "まだ実験を実行していません" },
}.freeze
SHARED_KEYS = %w[experiment_id slug priority status issue issue_url measured_at code_commit run_ids cleanup_verified
                 remaining_resources].freeze
PAGES = { "decision" => ["Conclusion", "결론", "結論"], "guide" => ["Rules for coding agents", "에이전트용 규칙", "コーディングエージェント向けルール"] }.freeze

root = File.expand_path("..", __dir__)
errors = []
stray = Dir.glob(File.join(root, "docs/_experiments/*.md"))
errors << "Reports must live in docs/_experiments/<lang>/: #{stray.map { |f| File.basename(f) }.join(', ')}" unless stray.empty?

reports = {}
LANGS.each do |lang, rules|
  files = Dir.glob(File.join(root, "docs/_experiments/#{lang}/*.md")).sort
  errors << "#{lang}: no experiment reports found" if files.empty?
  identifiers = []
  files.each do |file|
    name = "#{lang}/#{File.basename(file)}"
    source = File.read(file)
    front_matter = source.match(/\A---\r?\n(.*?)\r?\n---\r?\n/m)
    unless front_matter
      errors << "#{name}: missing YAML front matter"
      next
    end
    begin
      data = YAML.safe_load(front_matter[1], permitted_classes: [Date])
    rescue Psych::Exception => e
      errors << "#{name}: #{e.message}"
      next
    end
    unless data.is_a?(Hash)
      errors << "#{name}: metadata must be a mapping"
      next
    end
    reports[[lang, File.basename(file, ".md")]] = data
    %w[experiment_id slug title question priority status issue_url].each do |key|
      errors << "#{name}: missing #{key}" unless data[key].is_a?(String) && !data[key].strip.empty?
    end
    errors << "#{name}: lang must be #{lang}" unless data["lang"] == lang
    identifier = data["experiment_id"].to_s
    identifiers << identifier
    errors << "#{name}: invalid experiment_id" unless identifier.match?(/\AE\d{3}\z/)
    errors << "#{name}: slug must match file name and experiment ID" unless data["slug"] == File.basename(file, ".md") && data["slug"] == identifier.downcase
    errors << "#{name}: permalink must be #{rules[:prefix]}/experiments/#{data['slug']}/" unless data["permalink"] == "#{rules[:prefix]}/experiments/#{data['slug']}/"
    errors << "#{name}: invalid status" unless %w[planned running completed].include?(data["status"])
    errors << "#{name}: invalid priority" unless %w[P0 P1 P2].include?(data["priority"])
    issue = data["issue"]
    expected_url = "https://github.com/roboco-io/dsql-experiments/issues/#{issue}"
    errors << "#{name}: invalid issue link" unless issue.is_a?(Integer) && issue.positive? && data["issue_url"] == expected_url
    errors << "#{name}: do not use '~' (GFM strikethrough)" if source.include?("~")
    next unless data["status"] == "completed"

    %w[summary result measured_at code_commit].each do |key|
      errors << "#{name}: completed reports require #{key}" if data[key].to_s.strip.empty?
    end
    begin
      Date.iso8601(data["measured_at"].to_s)
    rescue Date::Error
      errors << "#{name}: measured_at must be an ISO date"
    end
    errors << "#{name}: code_commit must be a full Git SHA" unless data["code_commit"].to_s.match?(/\A[0-9a-f]{40}\z/)
    runs = data["run_ids"]
    errors << "#{name}: completed reports require run_ids" unless runs.is_a?(Array) && !runs.empty? && runs.all? { |run| run.is_a?(String) && !run.strip.empty? }
    errors << "#{name}: cleanup must be verified with zero remaining resources" unless data["cleanup_verified"] == true && data["remaining_resources"] == 0
    body = source[front_matter.end(0)..]
    view = body[/^### #{Regexp.escape(rules[:production])}\s*\n(.*?)(?=^#)/m, 1]
    if view.nil?
      errors << "#{name}: #{rules[:summary]} needs a '### #{rules[:production]}' paragraph"
    elsif view.strip.length > rules[:limit]
      errors << "#{name}: #{rules[:production]} must be at most #{rules[:limit]} characters (#{view.strip.length})"
    end
    rules[:sections].each do |heading|
      errors << "#{name}: missing result section #{heading}" unless body.match?(/^## #{Regexp.escape(heading)}\s*$/)
    end
    errors << "#{name}: remove the unmeasured placeholder before completing" if body.include?(rules[:placeholder])
  end
  errors << "#{lang}: experiment IDs must be unique" unless identifiers.uniq == identifiers
end

# Every language carries the same experiments with the same shared metadata as the Korean source.
slugs = LANGS.keys.to_h { |lang| [lang, reports.keys.select { |l, _| l == lang }.map(&:last).sort] }
LANGS.each_key do |lang|
  next if slugs[lang] == slugs["ko"]

  errors << "#{lang}: reports #{slugs[lang].inspect} do not match the Korean source #{slugs['ko'].inspect}"
end
slugs["ko"].each do |slug|
  source = reports[["ko", slug]]
  (LANGS.keys - ["ko"]).each do |lang|
    other = reports[[lang, slug]]
    next unless other

    SHARED_KEYS.each do |key|
      errors << "#{lang}/#{slug}.md: #{key} differs from the Korean source" unless other[key] == source[key]
    end
  end
end

# The decision report and the usage guide exist in every language.
PAGES.each do |page, openers|
  LANGS.each_with_index do |(lang, rules), index|
    path = File.join(root, "docs", lang == "en" ? "#{page}.md" : "#{lang}/#{page}.md")
    unless File.exist?(path)
      errors << "Missing #{lang} #{page} page"
      next
    end
    text = File.read(path)
    errors << "#{lang} #{page}: permalink must be #{rules[:prefix]}/#{page}/" unless text.match?(%r{^permalink: #{Regexp.escape(rules[:prefix])}/#{page}/$})
    errors << "#{lang} #{page}: lang must be #{lang}" unless text.match?(/^lang: "?#{lang}"?$/)
    errors << "#{lang} #{page}: missing '## #{openers[index]}'" unless text.match?(/^## #{Regexp.escape(openers[index])}\s*$/)
    errors << "#{lang} #{page}: do not use '~'" if text.include?("~")
  end
end

abort errors.join("\n") unless errors.empty?
puts "Validated #{reports.size} experiment reports in #{LANGS.size} languages"

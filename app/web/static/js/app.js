/*
 * Comportamentos do portal CarbonLedger. Arquivo unico, sem dependencia de dominio externo.
 * - CSRF em toda requisicao HTMX (token da meta csrf-token).
 * - Aviso de resultado de toda acao HTMX numa regiao aria-live.
 * - Validacao na hora: o servidor devolve a mensagem do campo; aqui so marcamos o campo.
 * - Rascunho: o que foi digitado fica no navegador ate o envio dar certo.
 * - Confirmacao por digitacao em acao irreversivel.
 * - Glossario (popover) e graficos (ECharts, valores em pt-BR).
 */
(function () {
  "use strict";

  var PREFIXO_RASCUNHO = "carbonledger:rascunho:";

  // O Tabler 1.7 expoe os componentes do Bootstrap em window.tabler.
  function componentes() {
    return window.tabler || window.bootstrap || null;
  }
  var formatoNumero = new Intl.NumberFormat("pt-BR", { maximumFractionDigits: 2 });

  // ---------------------------------------------------------------- avisos (aria-live)

  function avisar(texto, tipo) {
    var regiao = document.getElementById("cl-aviso");
    if (!regiao || !texto) {
      return;
    }
    var aviso = document.createElement("div");
    aviso.className = "alert alert-" + (tipo || "success") + " mb-2";
    aviso.textContent = texto;
    regiao.appendChild(aviso);
    window.setTimeout(function () {
      aviso.remove();
    }, 7000);
  }

  function mensagemDeErro(xhr) {
    try {
      var corpo = JSON.parse(xhr.responseText);
      if (corpo && corpo.erros && corpo.erros.length > 1) {
        return "Corrija os " + corpo.erros.length + " campos destacados e envie de novo.";
      }
      if (corpo && corpo.erros && corpo.erros.length) {
        return corpo.erros[0].mensagem;
      }
    } catch (ignorado) {
      // corpo nao e JSON: usa a mensagem padrao
    }
    if (xhr.status === 404) {
      return "Não encontrado ou sem permissão. Atualize a página e tente de novo.";
    }
    if (xhr.status === 0) {
      return "Sem conexão com o servidor. Confira a internet e tente de novo.";
    }
    return "Não foi possível concluir agora. Tente de novo em instantes.";
  }

  function marcarErrosDoFormulario(form, xhr) {
    try {
      var corpo = JSON.parse(xhr.responseText);
      (corpo.erros || []).forEach(function (erro) {
        var alvo = form.querySelector("#erro-" + erro.campo);
        if (alvo) {
          alvo.textContent = erro.mensagem;
          sincronizarCampo(alvo);
        }
      });
    } catch (ignorado) {
      // sem detalhes por campo
    }
  }

  // ---------------------------------------------------------------- campos

  function sincronizarCampo(mensagem) {
    var campo = document.getElementById(mensagem.getAttribute("data-erro-de"));
    if (!campo) {
      return;
    }
    var temErro = mensagem.textContent.trim() !== "";
    campo.classList.toggle("is-invalid", temErro);
    if (temErro) {
      campo.setAttribute("aria-invalid", "true");
    } else {
      campo.removeAttribute("aria-invalid");
    }
  }

  // ---------------------------------------------------------------- rascunho

  function chaveRascunho(form) {
    return PREFIXO_RASCUNHO + form.getAttribute("data-rascunho");
  }

  function salvarRascunho(form) {
    var dados = {};
    Array.prototype.forEach.call(form.elements, function (campo) {
      if (campo.name && campo.type !== "password" && campo.type !== "file") {
        dados[campo.name] = campo.value;
      }
    });
    try {
      window.localStorage.setItem(chaveRascunho(form), JSON.stringify(dados));
    } catch (ignorado) {
      // armazenamento cheio ou bloqueado: segue sem rascunho
    }
  }

  function restaurarRascunho(form) {
    var bruto = null;
    try {
      bruto = window.localStorage.getItem(chaveRascunho(form));
    } catch (ignorado) {
      return;
    }
    if (!bruto) {
      return;
    }
    var dados = JSON.parse(bruto);
    var restaurou = false;
    Object.keys(dados).forEach(function (nome) {
      var campo = form.elements.namedItem(nome);
      if (campo && "value" in campo && campo.value !== dados[nome]) {
        campo.value = dados[nome];
        restaurou = true;
      }
    });
    if (restaurou) {
      avisar("Recuperamos o que você tinha digitado e ainda não foi salvo.", "info");
    }
  }

  function apagarRascunho(form) {
    try {
      window.localStorage.removeItem(chaveRascunho(form));
    } catch (ignorado) {
      // nada a apagar
    }
  }

  // ---------------------------------------------------------------- confirmacao

  function atualizarConfirmacao(entrada) {
    var botao = document.getElementById(entrada.getAttribute("data-confirmar-botao"));
    if (botao) {
      botao.disabled = entrada.value.trim() !== entrada.getAttribute("data-confirmar-texto");
    }
  }

  // ---------------------------------------------------------------- graficos

  function coresDoTema() {
    var estilo = window.getComputedStyle(document.documentElement);
    return ["--cl-indigo-700", "--cl-marinho-700", "--cl-info", "--cl-sucesso", "--cl-alerta"]
      .map(function (nome) {
        return estilo.getPropertyValue(nome).trim();
      })
      .filter(function (cor) {
        return cor !== "";
      });
  }

  function desenharGraficos(raiz) {
    if (!window.echarts) {
      return;
    }
    raiz.querySelectorAll("[data-grafico]").forEach(function (alvo) {
      if (alvo.getAttribute("data-desenhado")) {
        return;
      }
      var fonte = document.getElementById(alvo.getAttribute("data-grafico"));
      if (!fonte) {
        return;
      }
      var opcoes = JSON.parse(fonte.textContent);
      opcoes.aria = { enabled: true };
      // Quem pede menos movimento no sistema nao ve animacao de entrada.
      if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
        opcoes.animation = false;
      }
      // Cores so do tema.css: o grafico le as variaveis CSS, nunca uma cor fixa.
      if (!opcoes.color) {
        opcoes.color = coresDoTema();
      }
      opcoes.tooltip = Object.assign({ trigger: "axis" }, opcoes.tooltip || {});
      opcoes.tooltip.valueFormatter = function (valor) {
        return formatoNumero.format(Number(valor));
      };
      (opcoes.yAxis ? [].concat(opcoes.yAxis) : []).forEach(function (eixo) {
        eixo.axisLabel = Object.assign({}, eixo.axisLabel || {}, {
          formatter: function (valor) {
            return formatoNumero.format(valor);
          }
        });
      });
      var grafico = window.echarts.init(alvo, null, { renderer: "canvas" });
      grafico.setOption(opcoes);
      alvo.setAttribute("data-desenhado", "1");
      window.addEventListener("resize", function () {
        grafico.resize();
      });
    });
  }

  // ---------------------------------------------------------------- inicializacao

  function iniciar(raiz) {
    raiz.querySelectorAll("form[data-rascunho]").forEach(restaurarRascunho);
    raiz.querySelectorAll("[data-erro-de]").forEach(sincronizarCampo);
    var bs = componentes();
    if (bs && bs.Popover) {
      raiz.querySelectorAll('[data-bs-toggle="popover"]').forEach(function (elemento) {
        bs.Popover.getOrCreateInstance(elemento);
      });
    }
    desenharGraficos(raiz);
  }

  document.addEventListener("DOMContentLoaded", function () {
    iniciar(document);
  });

  document.addEventListener("input", function (evento) {
    var alvo = evento.target;
    if (alvo.matches && alvo.matches("[data-confirmar-texto]")) {
      atualizarConfirmacao(alvo);
    }
    var form = alvo.form;
    if (form && form.hasAttribute("data-rascunho")) {
      salvarRascunho(form);
    }
  });

  document.addEventListener("hidden.bs.modal", function (evento) {
    evento.target.querySelectorAll("[data-confirmar-texto]").forEach(function (entrada) {
      entrada.value = "";
      atualizarConfirmacao(entrada);
    });
  });

  document.addEventListener("htmx:configRequest", function (evento) {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) {
      evento.detail.headers["X-CSRF-Token"] = meta.getAttribute("content");
    }
  });

  document.addEventListener("htmx:afterSwap", function (evento) {
    var alvo = evento.detail.target;
    if (alvo && alvo.hasAttribute && alvo.hasAttribute("data-erro-de")) {
      sincronizarCampo(alvo);
    }
    var novo = document.getElementById(alvo && alvo.id);
    if (novo && novo.hasAttribute("data-erro-de")) {
      sincronizarCampo(novo);
    }
    iniciar(evento.detail.elt.parentElement || document);
  });

  document.addEventListener("htmx:afterRequest", function (evento) {
    var xhr = evento.detail.xhr;
    var elemento = evento.detail.elt;
    var validacao = elemento && elemento.hasAttribute && elemento.hasAttribute("hx-trigger") &&
      elemento.matches("input, select, textarea");
    if (validacao) {
      return; // validacao de campo: a mensagem ja aparece junto do campo
    }
    var form = elemento && elemento.tagName === "FORM" ? elemento : null;
    if (evento.detail.successful) {
      var aviso = xhr.getResponseHeader("X-Aviso");
      avisar(aviso ? decodeURIComponent(aviso) : "Pronto.", "success");
      if (form && form.hasAttribute("data-rascunho")) {
        apagarRascunho(form);
      }
      var modal = elemento.closest && elemento.closest(".modal");
      var bs = componentes();
      if (modal && bs && bs.Modal) {
        bs.Modal.getOrCreateInstance(modal).hide();
      }
    } else {
      if (form) {
        marcarErrosDoFormulario(form, xhr);
      }
      avisar(mensagemDeErro(xhr), "danger");
    }
  });
})();

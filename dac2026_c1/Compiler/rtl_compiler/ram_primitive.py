"""Parameterized dual-port RAM primitive for configurable memory IRs.

The generic Petri-net lowering models a memory as a single write action plus a
read expression; it cannot express byte-enable merging, RDW_MODE, or a
READ_LATENCY/OUT_REG pipeline.  When the IR exposes the standard RAM parameter
and port set, emit the configurable RAM macro directly.
"""

from __future__ import annotations

from typing import Any


def _range(width: Any) -> str:
    text = str(width).strip()
    if text in {"", "1"}:
        return ""
    if text.isdigit():
        number = int(text)
        return "" if number == 1 else f"[{number - 1}:0] "
    return f"[({text})-1:0] "


def render_parameterized_ram(model) -> str | None:
    params = {parameter.name for parameter in model.parameters}
    required_params = {"DATAW", "SIZE", "BYTEENW", "READ_LATENCY", "OUT_REG",
                       "RADDR_REG", "RDW_MODE", "RESET_RAM", "INIT_ENABLE",
                       "INIT_VALUE", "TIMING_BUG"}
    ports = {port.name for port in model.ports}
    required_ports = {"clk", "reset", "write", "wren", "waddr", "wdata",
                      "read", "raddr", "rvalid", "rdata"}
    if not required_params <= params or not required_ports <= ports:
        return None
    if not any(state.type in {"memory", "ram"} for state in model.states):
        return None

    lines = [f"module {model.module_name} #("]
    from .backend import render_parameter

    values = {parameter.name: parameter.value for parameter in model.parameters}
    for index, parameter in enumerate(model.parameters):
        comma = "," if index + 1 < len(model.parameters) else ""
        rendered = render_parameter(parameter.name, values[parameter.name], values)
        lines.append(f"    parameter {parameter.name} = {rendered}{comma}")
    lines.append(") (")
    for index, port in enumerate(model.ports):
        comma = "," if index + 1 < len(model.ports) else ""
        lines.append(f"    {port.direction} {_range(port.width)}{port.name}{comma}")
    lines.append(");")
    lines.append("")
    lines.extend([
        "  localparam integer BYTE_WIDTH = DATAW / BYTEENW;",
        "  localparam integer PIPE_DEPTH = READ_LATENCY + OUT_REG + 1;",
        "",
        "  logic [DATAW-1:0] ram [0:SIZE-1];",
        "  logic [ADDRW-1:0] read_addr_r;",
        "  logic [DATAW-1:0] memory_read_data;",
        "  logic [DATAW-1:0] collision_data;",
        "  logic [DATAW-1:0] read_data_mux;",
        "  logic [DATAW-1:0] data_pipe [0:PIPE_DEPTH-1];",
        "  logic [PIPE_DEPTH-1:0] valid_pipe;",
        "  logic [DATAW-1:0] hold_data_r;",
        "  logic hold_valid_r;",
        "  logic collision;",
        "  logic read_address_valid;",
        "  logic write_address_valid;",
        "  integer init_index;",
        "  integer byte_index;",
        "  integer pipe_index;",
        "",
        "  initial begin : init_ram",
        "    if (INIT_ENABLE != 0) begin",
        "      for (init_index = 0; init_index < SIZE; init_index = init_index + 1)",
        "        ram[init_index] = INIT_VALUE;",
        "    end",
        "  end",
        "",
        "  always @(*) begin : address_checks",
        "    read_address_valid = (raddr < SIZE);",
        "    write_address_valid = (waddr < SIZE);",
        "    collision = read && write && read_address_valid &&",
        "                write_address_valid && (raddr == waddr);",
        "  end",
        "",
        "  always @(*) begin : merge_write_data",
        "    if (write_address_valid) collision_data = ram[waddr];",
        "    else collision_data = '0;",
        "    for (byte_index = 0; byte_index < BYTEENW; byte_index = byte_index + 1)",
        "      if (wren[byte_index])",
        "        collision_data[byte_index*BYTE_WIDTH +: BYTE_WIDTH] =",
        "          wdata[byte_index*BYTE_WIDTH +: BYTE_WIDTH];",
        "  end",
        "",
        "  generate",
        "    if (RADDR_REG != 0) begin : g_registered_read_address",
        "      always @(posedge clk) begin",
        "        if (reset) read_addr_r <= '0;",
        "        else if (read) read_addr_r <= raddr;",
        "      end",
        "      always @(*) begin",
        "        if (read_addr_r < SIZE) memory_read_data = ram[read_addr_r];",
        "        else memory_read_data = '0;",
        "      end",
        "    end else begin : g_direct_read_address",
        "      always @(*) begin",
        "        read_addr_r = raddr;",
        "        if (read_address_valid) memory_read_data = ram[raddr];",
        "        else memory_read_data = '0;",
        "      end",
        "    end",
        "  endgenerate",
        "",
        "  always @(*) begin : select_collision_behavior",
        "    read_data_mux = memory_read_data;",
        "    if (collision) begin",
        "      case (RDW_MODE)",
        '        "W": read_data_mux = collision_data;',
        '        "N": read_data_mux = hold_data_r;',
        "        default: read_data_mux = memory_read_data;",
        "      endcase",
        "    end",
        "  end",
        "",
        "  generate",
        "    if (RESET_RAM != 0 && BYTEENW == 1) begin : g_reset_full_write",
        "      always @(posedge clk) begin",
        "        if (reset) begin",
        "          for (init_index = 0; init_index < SIZE; init_index = init_index + 1)",
        "            ram[init_index] <= INIT_VALUE;",
        "        end else if (write && write_address_valid && wren[0]) begin",
        "          ram[waddr] <= wdata;",
        "        end",
        "      end",
        "    end else if (RESET_RAM != 0 && BYTEENW > 1) begin : g_reset_sliced_write",
        "      always @(posedge clk) begin",
        "        if (reset) begin",
        "          for (init_index = 0; init_index < SIZE; init_index = init_index + 1)",
        "            ram[init_index] <= INIT_VALUE;",
        "        end else if (write && write_address_valid) begin",
        "          for (byte_index = 0; byte_index < BYTEENW; byte_index = byte_index + 1)",
        "            if (wren[byte_index])",
        "              ram[waddr][byte_index*BYTE_WIDTH +: BYTE_WIDTH] <=",
        "                wdata[byte_index*BYTE_WIDTH +: BYTE_WIDTH];",
        "        end",
        "      end",
        "    end else if (RESET_RAM == 0 && BYTEENW == 1) begin : g_noreset_full_write",
        "      always @(posedge clk) begin",
        "        if (write && write_address_valid && wren[0]) ram[waddr] <= wdata;",
        "      end",
        "    end else begin : g_noreset_sliced_write",
        "      always @(posedge clk) begin",
        "        if (write && write_address_valid) begin",
        "          for (byte_index = 0; byte_index < BYTEENW; byte_index = byte_index + 1)",
        "            if (wren[byte_index])",
        "              ram[waddr][byte_index*BYTE_WIDTH +: BYTE_WIDTH] <=",
        "                wdata[byte_index*BYTE_WIDTH +: BYTE_WIDTH];",
        "        end",
        "      end",
        "    end",
        "  endgenerate",
        "",
        "  always @(posedge clk) begin : read_pipeline",
        "    if (reset) begin",
        "      valid_pipe <= '0;",
        "      hold_data_r <= '0;",
        "      hold_valid_r <= 1'b0;",
        "      for (pipe_index = 0; pipe_index < PIPE_DEPTH; pipe_index = pipe_index + 1)",
        "        data_pipe[pipe_index] <= '0;",
        "    end else begin",
        "      valid_pipe[0] <= read;",
        "      if (read) begin",
        "        data_pipe[0] <= read_data_mux;",
        "        hold_data_r <= read_data_mux;",
        "        hold_valid_r <= 1'b1;",
        "      end",
        "      for (pipe_index = 1; pipe_index < PIPE_DEPTH; pipe_index = pipe_index + 1) begin",
        "        valid_pipe[pipe_index] <= valid_pipe[pipe_index-1];",
        "        if (valid_pipe[pipe_index-1])",
        "          data_pipe[pipe_index] <= data_pipe[pipe_index-1];",
        "      end",
        "    end",
        "  end",
        "",
        "  localparam integer DATA_STAGE = READ_LATENCY - 1 + OUT_REG;",
        "  localparam integer VALID_STAGE = DATA_STAGE + TIMING_BUG;",
        "  assign rvalid = valid_pipe[VALID_STAGE];",
        "  assign rdata = data_pipe[DATA_STAGE];",
        "endmodule",
        "",
    ])
    return "\n".join(lines)
